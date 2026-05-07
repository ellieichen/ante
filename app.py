import os
import io
import base64
import xml.sax.saxutils
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, make_response
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


def _twilio_client():
    """Returns a Twilio Client if configured, else None. Importing inside the
    function so the app still boots without the twilio package installed."""
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
        return None
    try:
        from twilio.rest import Client
        return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except ImportError:
        return None


def _send_sms(to_phone, body, bid_id_for_log=''):
    """Single low-level SMS send. No-op if Twilio isn't configured or phone is invalid."""
    client = _twilio_client()
    if not client:
        app.logger.info(f"SMS skipped (Twilio not configured): to={to_phone} body={body!r}")
        return False
    to_number = _normalize_phone(to_phone or '')
    if not to_number:
        return False
    try:
        client.messages.create(from_=TWILIO_FROM_NUMBER, to=to_number, body=body)
        return True
    except Exception as e:
        app.logger.warning(f"Twilio SMS failed (bid={bid_id_for_log}): {e}")
        return False


def _twiml_reply(msg):
    """Build a TwiML SMS reply Twilio will send back to the user."""
    escaped = xml.sax.saxutils.escape(msg)
    body = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escaped}</Message></Response>'
    return body, 200, {'Content-Type': 'application/xml'}


def _release_stripe_hold(bid):
    """Cancel a Stripe PaymentIntent hold and mark the bid released. Best-effort."""
    if bid['stripe_payment_intent'] and stripe.api_key:
        try:
            stripe.PaymentIntent.cancel(bid['stripe_payment_intent'])
            models.mark_bid_released(bid['id'])
        except stripe.error.StripeError as e:
            app.logger.warning(f"Could not release Stripe hold for {bid['id']}: {e}")


def _capture_stripe_hold(bid):
    """Capture a Stripe PaymentIntent. Best-effort — failure is logged but not raised."""
    if bid['stripe_payment_intent'] and stripe.api_key:
        try:
            stripe.PaymentIntent.capture(bid['stripe_payment_intent'])
        except stripe.error.StripeError as e:
            app.logger.error(f"Stripe capture failed for {bid['id']}: {e}")


def _promote_next_runner_up(session_id):
    """When a winner forfeits, find the highest-ranked outbid bidder who opted
    in to auto-promote and offer them the table at their original bid amount.
    Walks one bidder at a time; if they later forfeit, the next call promotes
    the bidder after them. No-op if nobody qualifies (admin handles manually)."""
    candidate = models.find_next_promotable_runner_up(session_id)
    if not candidate:
        app.logger.info(f"Session {session_id}: no opted-in runner-up to promote")
        return
    if not models.promote_outbid_to_pending_confirm(candidate['id']):
        app.logger.info(f"Promotion lost the race for bid {candidate['id']}")
        return
    venue = models.get_venue(models.get_session(session_id)['venue_id'])
    promoted = models.get_bid(candidate['id'])  # re-read to get fresh status + timestamp
    _send_confirmation_prompt(promoted, venue)
    app.logger.info(f"Promoted bid {promoted['id']} ({promoted['bidder_name']}) to won_pending_confirm")


def _process_expirations():
    """Lazy timer tick: forfeit winners past their 10-min confirm window AND
    expire pending bids over an hour old. Releases Stripe holds in both cases
    and triggers runner-up promotion for forfeits. Called from bid status,
    session, and admin pages — no background scheduler needed for the POC."""
    for bid in models.expire_stale_confirm_windows():
        _release_stripe_hold(bid)
        _promote_next_runner_up(bid['session_id'])
    for bid in models.expire_stale_pending_bids():
        _release_stripe_hold(bid)


def _send_confirmation_prompt(bid, venue):
    """Text a pending-confirm winner asking them to reply Y or N. Used both at
    initial close and when promoting a runner-up after a forfeit."""
    body = (
        f"ANTE: You've secured your table at {venue['name']}! "
        f"Please confirm with Y or N to cancel. "
        f"You have 10 minutes until the table is given to the next highest bidder."
    )
    return _send_sms(bid['bidder_phone'], body, bid_id_for_log=bid['id'])


# ─── Landing Page ──────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


# ─── Customer: View Session & Place Bid ────────────────────────

@app.route('/e/<session_id>')
def session_page(session_id):
    """Customer-facing bidding page (reached via QR code at the venue)."""
    _process_expirations()
    session = models.get_session(session_id)
    if not session:
        flash('Bidding session not found.', 'error')
        return redirect(url_for('index'))
    # If the bidder has already placed a bid in this session and is just
    # navigating back here (e.g. scanned the QR again), don't dump them on the
    # place-a-bid form — send them to their bid status where they can up the
    # ante or release. The "up the ante" path explicitly carries ?from_bid=.
    from_bid_id = request.args.get('from_bid')
    if not from_bid_id:
        cookie_bid_id = request.cookies.get(f'ante_bid_{session_id}')
        if cookie_bid_id:
            existing = models.get_bid(cookie_bid_id)
            if existing and existing['session_id'] == session_id and existing['status'] == 'pending':
                return redirect(url_for('bid_status', bid_id=cookie_bid_id))

    venue = models.get_venue(session['venue_id'])
    stats = models.get_bid_stats(session_id)

    from_bid = models.get_bid(from_bid_id) if from_bid_id else None
    if from_bid and (from_bid['session_id'] != session_id or from_bid['status'] != 'pending'):
        from_bid = None

    return render_template('event.html', session=session, venue=venue, stats=stats,
                           stripe_key=STRIPE_PUBLISHABLE_KEY,
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

    # Create a Stripe Customer + set setup_future_usage so the same card can
    # be reused server-side when this bidder ups the ante. Without this, the
    # off_session PaymentIntent in /up-bid would be rejected by Stripe.
    try:
        customer = stripe.Customer.create(
            email=email,
            name=name,
            metadata={'session_id': session_id},
        )
        intent = stripe.PaymentIntent.create(
            amount=amount * 100,
            currency='usd',
            capture_method='manual',
            payment_method_types=['card'],
            customer=customer.id,
            setup_future_usage='off_session',
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
    auto_promote = bool(data.get('auto_promote', True))

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

    bid_id = models.place_bid(session_id, name, email, phone, amount,
                              stripe_payment_intent=payment_intent_id,
                              auto_promote=auto_promote)

    # Drop a cookie scoped to this session so the bidder page can highlight
    # "you" when this bidder revisits /e/<session_id>. Updated on each up-the-ante.
    response_cookie_bid_id = bid_id

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

    resp = jsonify({'bid_id': bid_id, 'redirect': f'/bid/{bid_id}'})
    resp.set_cookie(
        f'ante_bid_{session_id}', response_cookie_bid_id,
        max_age=4 * 60 * 60, httponly=True, samesite='Lax'
    )
    return resp


@app.route('/bid/<bid_id>/release', methods=['POST'])
def release_bid(bid_id):
    """Bidder-initiated cancel. Atomically flips pending → cancelled and releases
    the Stripe authorization hold. Idempotent: if the bid already moved to
    another state (won_pending_confirm, outbid, etc.), the cancel fails cleanly."""
    bid = models.get_bid(bid_id)
    if not bid:
        flash('Bid not found.', 'error')
        return redirect(url_for('index'))

    if not models.transition_bid_status(bid_id, 'pending', 'cancelled'):
        flash('This bid is no longer active and cannot be released.', 'error')
        return redirect(url_for('bid_status', bid_id=bid_id))

    if bid['stripe_payment_intent'] and stripe.api_key:
        try:
            stripe.PaymentIntent.cancel(bid['stripe_payment_intent'])
            models.mark_bid_released(bid_id)
        except stripe.error.StripeError as e:
            app.logger.warning(f"Could not release Stripe hold for {bid_id}: {e}")

    flash('Your bid has been released. The hold on your card will clear shortly.', 'success')
    return redirect(url_for('bid_status', bid_id=bid_id))


@app.route('/e/<session_id>/up-bid', methods=['POST'])
def up_bid(session_id):
    """Up-the-ante shortcut. Reuses the card already authorized on the original
    bid so the bidder only edits the amount — no card re-entry, no second
    Stripe Elements step. Atomically cancels the old bid and creates a new one
    at the higher amount. The browser never touches Stripe in this path."""
    session = models.get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found.'}), 404
    if session['status'] != 'open':
        return jsonify({'error': 'Bidding is closed.'}), 400

    data = request.get_json() or {}
    from_bid_id = (data.get('from_bid') or '').strip()
    try:
        new_amount = int(data.get('amount', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid amount.'}), 400

    if not from_bid_id:
        return jsonify({'error': 'Missing original bid reference.'}), 400

    old_bid = models.get_bid(from_bid_id)
    if not old_bid or old_bid['session_id'] != session_id or old_bid['status'] != 'pending':
        return jsonify({'error': 'Original bid is no longer eligible to up.'}), 400

    if new_amount <= old_bid['amount']:
        return jsonify({'error': f'Bid more than your current ${old_bid["amount"]}.'}), 400
    if new_amount < session['min_bid']:
        return jsonify({'error': f'Minimum bid is ${session["min_bid"]}.'}), 400
    if new_amount > 10000:
        return jsonify({'error': 'Maximum bid is $10,000.'}), 400

    if not stripe.api_key:
        return jsonify({'error': 'Payment processing is not configured.'}), 500
    if not old_bid['stripe_payment_intent']:
        return jsonify({'error': 'Original bid has no payment record. Place a fresh bid instead.'}), 400

    # Pull the payment method from the original PaymentIntent so we can authorize
    # the new amount without prompting the user for the card again.
    try:
        old_intent = stripe.PaymentIntent.retrieve(old_bid['stripe_payment_intent'])
    except stripe.error.StripeError as e:
        return jsonify({'error': f'Could not load original bid: {e.user_message or str(e)}'}), 400

    payment_method_id = old_intent.payment_method
    if not payment_method_id:
        return jsonify({'error': 'No saved card found on the original bid.'}), 400

    try:
        new_intent = stripe.PaymentIntent.create(
            amount=new_amount * 100,
            currency='usd',
            capture_method='manual',
            payment_method=payment_method_id,
            confirm=True,
            off_session=True,
            customer=old_intent.customer,
            metadata={
                'session_id': session_id,
                'up_from_bid': from_bid_id,
                'bidder_email': old_bid['bidder_email'],
            },
            description=f'ANTE up-the-ante at {models.get_venue(session["venue_id"])["name"]}',
        )
    except stripe.error.CardError as e:
        return jsonify({'error': f'Card declined: {e.user_message or str(e)}'}), 400
    except stripe.error.StripeError as e:
        return jsonify({'error': f'Payment authorization failed: {e.user_message or str(e)}'}), 400

    new_bid_id = models.place_bid(
        session_id,
        old_bid['bidder_name'], old_bid['bidder_email'], old_bid['bidder_phone'],
        new_amount,
        stripe_payment_intent=new_intent.id,
        auto_promote=True,
    )

    # Atomically retire the old bid + release its hold. transition_bid_status
    # protects against the race where the session closes between our checks.
    if models.transition_bid_status(from_bid_id, 'pending', 'cancelled'):
        try:
            stripe.PaymentIntent.cancel(old_bid['stripe_payment_intent'])
            models.mark_bid_released(from_bid_id)
        except stripe.error.StripeError as e:
            app.logger.warning(f"Could not release old hold for {from_bid_id}: {e}")

    resp = jsonify({'bid_id': new_bid_id, 'redirect': f'/bid/{new_bid_id}'})
    resp.set_cookie(
        f'ante_bid_{session_id}', new_bid_id,
        max_age=4 * 60 * 60, httponly=True, samesite='Lax'
    )
    return resp


@app.route('/bid/<bid_id>')
def bid_status(bid_id):
    _process_expirations()
    bid = models.get_bid(bid_id)
    if not bid:
        flash('Bid not found.', 'error')
        return redirect(url_for('index'))
    session = models.get_session(bid['session_id'])
    venue = models.get_venue(session['venue_id'])
    stats = models.get_bid_stats(bid['session_id'])

    # Rank this bid among active bids in the session (sorted: amount DESC, created_at ASC)
    all_bids = models.get_bids_for_session(bid['session_id'], include_inactive=False)
    rank = next((i + 1 for i, b in enumerate(all_bids) if b['id'] == bid_id), None)
    is_winning = rank is not None and rank <= session['spots_available']
    # Tied-with-higher-rank means another bidder above me has the same dollar amount but bid earlier
    tied_with_higher_rank = (
        rank is not None and rank > 1
        and all_bids[rank - 2]['amount'] == bid['amount']
    )

    resp = make_response(render_template(
        'bid_status.html', bid=bid, session=session, venue=venue, stats=stats,
        bids=all_bids, rank=rank, is_winning=is_winning,
        tied_with_higher_rank=tied_with_higher_rank
    ))
    # Identify this browser as the bidder so the public event page can
    # highlight their row when they navigate back to /e/<session_id>.
    resp.set_cookie(
        f'ante_bid_{bid["session_id"]}', bid_id,
        max_age=4 * 60 * 60, httponly=True, samesite='Lax'
    )
    return resp


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


@app.route('/admin/venue/<venue_id>/edit', methods=['GET', 'POST'])
def admin_edit_venue(venue_id):
    venue = models.get_venue(venue_id)
    if not venue:
        flash('Venue not found.', 'error')
        return redirect(url_for('admin_dashboard'))
    if request.method == 'POST':
        name = request.form['name'].strip()
        description = request.form.get('description', '').strip()
        location = request.form.get('location', '').strip()
        if not name:
            flash('Name is required.', 'error')
            return redirect(url_for('admin_edit_venue', venue_id=venue_id))
        models.update_venue(venue_id, name, description, location)
        flash(f'Saved changes to "{name}".', 'success')
        return redirect(url_for('admin_venue', venue_id=venue_id))
    return render_template('admin/edit_venue.html', venue=venue)


@app.route('/admin/venue/<venue_id>/delete', methods=['POST'])
def admin_delete_venue(venue_id):
    venue = models.get_venue(venue_id)
    if not venue:
        flash('Venue not found.', 'error')
        return redirect(url_for('admin_dashboard'))
    if not models.delete_venue(venue_id):
        flash(f'Cannot delete "{venue["name"]}" while a session is still open. Close it first.', 'error')
        return redirect(url_for('admin_venue', venue_id=venue_id))
    flash(f'Deleted "{venue["name"]}" and all its sessions.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/session/<session_id>/delete', methods=['POST'])
def admin_delete_session(session_id):
    session = models.get_session(session_id)
    if not session:
        flash('Session not found.', 'error')
        return redirect(url_for('admin_dashboard'))
    venue_id = session['venue_id']
    if not models.delete_session(session_id):
        flash('Cannot delete an open session. Close it first.', 'error')
        return redirect(url_for('admin_session', session_id=session_id))
    flash('Session deleted.', 'success')
    return redirect(url_for('admin_venue', venue_id=venue_id))


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
    _process_expirations()
    session = models.get_session(session_id)
    if not session:
        flash('Session not found.', 'error')
        return redirect(url_for('admin_dashboard'))

    _release_due_holds(session)
    session = models.get_session(session_id)  # re-read in case anything changed

    venue = models.get_venue(session['venue_id'])
    bids = models.get_bids_for_session(session_id)
    winners = models.get_active_winners(session_id)

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
                           winners=winners, qr_b64=qr_b64, qr_url=qr_url,
                           release_at=release_at,
                           hold_release_minutes=models.HOLD_RELEASE_MINUTES)


@app.route('/admin/session/<session_id>/close', methods=['POST'])
def admin_close_session(session_id):
    """Close bidding. Top N bidders enter a 10-minute confirm window — cards are
    NOT captured at close. Capture only happens when a winner replies Y by SMS.
    The rest are marked outbid and their holds release on the existing 90-min
    schedule (see _release_due_holds)."""
    session = models.get_session(session_id)
    if not session:
        return redirect(url_for('admin_dashboard'))

    if not models.close_session(session_id):
        flash('Bidding was already closed.', 'error')
        return redirect(url_for('admin_session', session_id=session_id))

    venue = models.get_venue(session['venue_id'])
    spots = session['spots_available']
    pending = models.get_pending_bids_in_rank_order(session_id)
    winners = pending[:spots]
    losers = pending[spots:]

    promoted_count = 0
    for bid in winners:
        if models.transition_to_pending_confirm(bid['id']):
            promoted_count += 1
            _send_confirmation_prompt(bid, venue)

    for bid in losers:
        models.transition_bid_status(bid['id'], 'pending', 'outbid')

    flash(f'Bidding closed. {promoted_count} winner(s) sent confirmation prompts.', 'success')
    return redirect(url_for('admin_session', session_id=session_id))


# ─── Twilio inbound webhook ────────────────────────────────────

@app.route('/sms/webhook', methods=['POST'])
def sms_webhook():
    """Twilio posts here when a bidder replies to a confirmation prompt.
    Y/Yes = confirm and capture the card. N/No = forfeit, release hold, and
    promote the next opted-in runner-up. Returns TwiML reply for Twilio to
    send back as an SMS acknowledgement.

    NOTE: Twilio request signature validation is not enforced. Before exposing
    this in production, validate against TWILIO_AUTH_TOKEN using
    twilio.request_validator.RequestValidator on request.url + request.form."""
    from_phone = _normalize_phone(request.form.get('From', ''))
    body = (request.form.get('Body', '') or '').strip().upper()

    bid = models.find_pending_confirm_bid_by_phone(from_phone) if from_phone else None
    if not bid:
        return _twiml_reply("ANTE: We don't see an active table reservation for this number.")

    session = models.get_session(bid['session_id'])
    venue = models.get_venue(session['venue_id'])

    if body in ('Y', 'YES', 'CONFIRM'):
        if models.transition_bid_status(bid['id'], 'won_pending_confirm', 'won_confirmed'):
            _capture_stripe_hold(bid)
            return _twiml_reply(
                f"ANTE: Confirmed. Head to the host stand at {venue['name']}. "
                f"Your ${bid['amount']} bid is credit toward your bill."
            )
        return _twiml_reply("ANTE: That confirmation could not be applied — the window may have ended.")

    if body in ('N', 'NO', 'CANCEL'):
        if models.transition_bid_status(bid['id'], 'won_pending_confirm', 'forfeited'):
            _release_stripe_hold(bid)
            _promote_next_runner_up(bid['session_id'])
            return _twiml_reply("ANTE: Released. The hold on your card has been cleared.")
        return _twiml_reply("ANTE: That release could not be applied — the window may have ended.")

    return _twiml_reply(
        f"ANTE: We didn't recognize that reply. Please reply Y to confirm or N to cancel "
        f"your table at {venue['name']}."
    )


# ─── Init & Run ────────────────────────────────────────────────

with app.app_context():
    models.init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5001)
