import os
import io
import base64
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from dotenv import load_dotenv
import stripe
import qrcode

import models

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'ante-dev-key-change-me')

stripe.api_key = os.getenv('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY = os.getenv('STRIPE_PUBLISHABLE_KEY', '')
BASE_URL = os.getenv('BASE_URL', 'http://localhost:5001')

TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN', '')
TWILIO_FROM_NUMBER = os.getenv('TWILIO_FROM_NUMBER', '')


def _normalize_phone(phone):
    """Best-effort normalize to E.164. Assumes US if 10 digits."""
    if not phone:
        return None
    if phone.strip().startswith('+'):
        return '+' + ''.join(c for c in phone if c.isdigit())
    digits = ''.join(c for c in phone if c.isdigit())
    if len(digits) == 10:
        return f'+1{digits}'
    if len(digits) == 11 and digits.startswith('1'):
        return f'+{digits}'
    return None


def _phone_valid(phone):
    return _normalize_phone(phone) is not None


def _send_winner_sms(bid, venue):
    """Text the winning bidder. No-op if Twilio isn't configured or phone invalid."""
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
        return
    to_number = _normalize_phone(bid['bidder_phone'] or '')
    if not to_number:
        return
    try:
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(
            from_=TWILIO_FROM_NUMBER,
            to=to_number,
            body=(f"ANTE: You won at {venue['name']}! Head back to the host stand — "
                  f"your ${bid['amount']} bid is credit toward your bill."),
        )
    except Exception as e:
        app.logger.warning(f"Twilio SMS failed for bid {bid['id']}: {e}")


# ─── Landing Page ──────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


# ─── Customer: View Session & Place Bid ────────────────────────

@app.route('/e/<session_id>')
def session_page(session_id):
    """Customer-facing bidding page (reached via QR code at the venue)."""
    session = models.get_session(session_id)
    if not session:
        flash('Bidding session not found.', 'error')
        return redirect(url_for('index'))
    venue = models.get_venue(session['venue_id'])
    stats = models.get_bid_stats(session_id)
    bids = models.get_bids_for_session(session_id)

    # "Up the ante" flow — pre-fill from a previous pending bid in this session
    from_bid_id = request.args.get('from_bid')
    from_bid = models.get_bid(from_bid_id) if from_bid_id else None
    if from_bid and (from_bid['session_id'] != session_id or from_bid['status'] != 'pending'):
        from_bid = None

    return render_template('event.html', session=session, venue=venue, stats=stats,
                           bids=bids, stripe_key=STRIPE_PUBLISHABLE_KEY,
                           from_bid=from_bid)


@app.route('/e/<session_id>/create-payment-intent', methods=['POST'])
def create_payment_intent(session_id):
    """Authorize a hold on the bidder's card. Capture happens only if they win."""
    session = models.get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    if session['status'] != 'open':
        return jsonify({'error': 'Bidding is closed'}), 400

    data = request.get_json()
    amount = int(data.get('amount', 0))
    name = data.get('name', '').strip()
    email = data.get('email', '').strip()
    phone = data.get('phone', '').strip()

    if amount < session['min_bid']:
        return jsonify({'error': f'Minimum bid is ${session["min_bid"]}'}), 400
    if amount > 10000:
        return jsonify({'error': 'Maximum bid is $10,000.'}), 400
    if not name or not email or not phone:
        return jsonify({'error': 'Name, email, and phone are required'}), 400
    if not _phone_valid(phone):
        return jsonify({'error': 'Please enter a valid phone number (e.g. (555) 123-4567).'}), 400

    # "Up the ante" — new bid must exceed the previous bid being replaced
    from_bid_id = data.get('from_bid', '').strip()
    if from_bid_id:
        old_bid = models.get_bid(from_bid_id)
        if not old_bid or old_bid['session_id'] != session_id or old_bid['status'] != 'pending':
            return jsonify({'error': 'Cannot up an invalid or already-resolved bid.'}), 400
        if amount <= old_bid['amount']:
            return jsonify({'error': f'Up the ante by bidding more than your current ${old_bid["amount"]}.'}), 400

    try:
        intent = stripe.PaymentIntent.create(
            amount=amount * 100,
            currency='usd',
            capture_method='manual',
            payment_method_types=['card'],
            metadata={
                'session_id': session_id,
                'bidder_name': name,
                'bidder_email': email,
            },
            description=f'ANTE at {models.get_venue(session["venue_id"])["name"]}',
        )
        return jsonify({
            'client_secret': intent.client_secret,
            'payment_intent_id': intent.id,
        })
    except stripe.error.StripeError as e:
        app.logger.error(f"Stripe PaymentIntent failed (session={session_id}, amount=${amount}): {e}")
        return jsonify({'error': str(e)}), 400


@app.route('/e/<session_id>/confirm-bid', methods=['POST'])
def confirm_bid(session_id):
    """Record the bid after Stripe authorization. Amount is sourced from Stripe, not the client."""
    session = models.get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    data = request.get_json()
    name = data.get('name', '').strip()
    email = data.get('email', '').strip()
    phone = data.get('phone', '').strip()
    payment_intent_id = data.get('payment_intent_id', '')

    if not phone or not _phone_valid(phone):
        return jsonify({'error': 'A valid phone number is required.'}), 400

    phone = _normalize_phone(phone)

    # Source of truth for amount is the authorized PaymentIntent, not the client.
    if not payment_intent_id:
        return jsonify({'error': 'Missing payment intent'}), 400
    try:
        intent = stripe.PaymentIntent.retrieve(payment_intent_id)
    except stripe.error.StripeError as e:
        return jsonify({'error': str(e)}), 400
    amount = intent.amount // 100

    # Guard against the close-during-confirm race: refund and reject if bidding closed
    # between the authorization and the bid being recorded.
    if session['status'] != 'open':
        try:
            stripe.PaymentIntent.cancel(payment_intent_id)
        except stripe.error.StripeError:
            pass
        return jsonify({'error': 'Bidding just closed. Your hold has been released.'}), 400

    bid_id = models.place_bid(session_id, name, email, phone, amount, payment_intent_id)

    # If "upping the ante", cancel the old bid and release its Stripe hold
    from_bid_id = data.get('from_bid', '').strip()
    if from_bid_id:
        old_bid = models.get_bid(from_bid_id)
        if old_bid and old_bid['session_id'] == session_id and old_bid['status'] == 'pending':
            models.update_bid_status(from_bid_id, 'cancelled')
            if old_bid['stripe_payment_intent']:
                try:
                    stripe.PaymentIntent.cancel(old_bid['stripe_payment_intent'])
                    models.mark_bid_released(from_bid_id)
                except stripe.error.StripeError as e:
                    app.logger.warning(f"Could not cancel old PaymentIntent for {from_bid_id}: {e}")

    return jsonify({'bid_id': bid_id, 'redirect': f'/bid/{bid_id}'})


@app.route('/bid/<bid_id>')
def bid_status(bid_id):
    bid = models.get_bid(bid_id)
    if not bid:
        flash('Bid not found.', 'error')
        return redirect(url_for('index'))
    session = models.get_session(bid['session_id'])
    venue = models.get_venue(session['venue_id'])
    stats = models.get_bid_stats(bid['session_id'])

    # Rank this bid among all bids in the session (sorted: amount DESC, created_at ASC)
    all_bids = models.get_bids_for_session(bid['session_id'])
    rank = next((i + 1 for i, b in enumerate(all_bids) if b['id'] == bid_id), None)
    is_winning = rank is not None and rank <= session['spots_available']
    # Tied-with-higher-rank means another bidder above me has the same dollar amount but bid earlier
    tied_with_higher_rank = (
        rank is not None and rank > 1
        and all_bids[rank - 2]['amount'] == bid['amount']
    )

    return render_template('bid_status.html', bid=bid, session=session, venue=venue, stats=stats,
                           bids=all_bids, rank=rank, is_winning=is_winning,
                           tied_with_higher_rank=tied_with_higher_rank)


# ─── Admin: Dashboard & Venues ─────────────────────────────────

@app.route('/admin')
def admin_dashboard():
    venues = models.get_all_venues()
    return render_template('admin/dashboard.html', venues=venues)


@app.route('/admin/venue/new', methods=['GET', 'POST'])
def admin_new_venue():
    if request.method == 'POST':
        name = request.form['name']
        description = request.form.get('description', '')
        location = request.form.get('location', '')
        venue_id = models.create_venue(name, description, location)
        flash(f'Venue "{name}" created!', 'success')
        return redirect(url_for('admin_venue', venue_id=venue_id))
    return render_template('admin/new_venue.html')


@app.route('/admin/venue/<venue_id>')
def admin_venue(venue_id):
    venue = models.get_venue(venue_id)
    if not venue:
        flash('Venue not found.', 'error')
        return redirect(url_for('admin_dashboard'))
    open_session = models.get_open_session_for_venue(venue_id)
    past_sessions = [s for s in models.get_sessions_for_venue(venue_id) if s['status'] == 'closed']
    return render_template('admin/venue.html', venue=venue,
                           open_session=open_session, past_sessions=past_sessions)


@app.route('/admin/venue/<venue_id>/open-session', methods=['POST'])
def admin_open_session(venue_id):
    venue = models.get_venue(venue_id)
    if not venue:
        flash('Venue not found.', 'error')
        return redirect(url_for('admin_dashboard'))
    if models.get_open_session_for_venue(venue_id):
        flash('There is already an open session for this venue.', 'error')
        return redirect(url_for('admin_venue', venue_id=venue_id))
    spots = max(1, int(request.form.get('spots_available', 1)))
    min_bid = max(1, int(request.form.get('min_bid', 50)))
    session_id = models.create_session(venue_id, spots, min_bid)
    flash('Bidding is now open!', 'success')
    return redirect(url_for('admin_session', session_id=session_id))


# ─── Admin: Session ────────────────────────────────────────────

def _release_due_holds(session):
    """Release Stripe holds for outbid bidders ≥90 minutes after the session closed."""
    if session['status'] != 'closed' or not session['closed_at']:
        return
    closed_at = datetime.strptime(session['closed_at'], '%Y-%m-%d %H:%M:%S')
    if datetime.utcnow() - closed_at < timedelta(minutes=models.HOLD_RELEASE_MINUTES):
        return
    for bid in models.get_outbid_unreleased(session['id']):
        if stripe.api_key:
            try:
                stripe.PaymentIntent.cancel(bid['stripe_payment_intent'])
            except stripe.error.StripeError:
                pass
        models.mark_bid_released(bid['id'])


@app.route('/admin/session/<session_id>')
def admin_session(session_id):
    session = models.get_session(session_id)
    if not session:
        flash('Session not found.', 'error')
        return redirect(url_for('admin_dashboard'))

    _release_due_holds(session)
    session = models.get_session(session_id)  # re-read in case anything changed

    venue = models.get_venue(session['venue_id'])
    bids = models.get_bids_for_session(session_id)
    winners, losers = models.get_winners_and_losers(session_id)

    qr_url = f'{BASE_URL}/e/{session_id}'
    qr = qrcode.make(qr_url, box_size=8, border=2)
    buf = io.BytesIO()
    qr.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    release_at = None
    if session['status'] == 'closed' and session['closed_at']:
        closed_at = datetime.strptime(session['closed_at'], '%Y-%m-%d %H:%M:%S')
        release_at = (closed_at + timedelta(minutes=models.HOLD_RELEASE_MINUTES)).strftime('%H:%M UTC')

    return render_template('admin/event.html', session=session, venue=venue, bids=bids,
                           winners=winners, losers=losers, qr_b64=qr_b64, qr_url=qr_url,
                           release_at=release_at,
                           hold_release_minutes=models.HOLD_RELEASE_MINUTES)


@app.route('/admin/session/<session_id>/close', methods=['POST'])
def admin_close_session(session_id):
    """Close bidding. Top N bidders win at their own bid amount (first-price)."""
    session = models.get_session(session_id)
    if not session:
        return redirect(url_for('admin_dashboard'))

    venue = models.get_venue(session['venue_id'])
    models.close_session(session_id)
    winners, losers = models.get_winners_and_losers(session_id)

    for bid in winners:
        models.update_bid_status(bid['id'], 'won')
        if bid['stripe_payment_intent'] and stripe.api_key:
            try:
                stripe.PaymentIntent.capture(bid['stripe_payment_intent'])
            except stripe.error.StripeError:
                pass
        _send_winner_sms(bid, venue)
    for bid in losers:
        models.update_bid_status(bid['id'], 'outbid')
        # Hold is released later (see _release_due_holds, runs ≥90min after close).

    flash(f'Bidding closed. {len(winners)} winner(s) selected.', 'success')
    return redirect(url_for('admin_session', session_id=session_id))


# ─── Init & Run ────────────────────────────────────────────────

with app.app_context():
    models.init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5001)
