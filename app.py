import os
import io
import base64
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from dotenv import load_dotenv
import stripe
import qrcode

import models

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'bid-for-credit-dev-key-change-me')

# Stripe config
stripe.api_key = os.getenv('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY = os.getenv('STRIPE_PUBLISHABLE_KEY', '')

# Base URL for QR codes (change when deploying)
BASE_URL = os.getenv('BASE_URL', 'http://localhost:5001')


# ─── Landing Page ──────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


# ─── Customer: View Event & Place Bid ──────────────────────────

@app.route('/e/<event_id>')
def event_page(event_id):
    """Customer-facing event page (reached via QR code)."""
    event = models.get_event(event_id)
    if not event:
        flash('Event not found.', 'error')
        return redirect(url_for('index'))
    venue = models.get_venue(event['venue_id'])
    stats = models.get_bid_stats(event_id)
    return render_template('event.html', event=event, venue=venue, stats=stats,
                           stripe_key=STRIPE_PUBLISHABLE_KEY)


@app.route('/e/<event_id>/create-payment-intent', methods=['POST'])
def create_payment_intent(event_id):
    """Create a Stripe PaymentIntent for the bid amount. Called by JS before showing payment form."""
    event = models.get_event(event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404
    if event['status'] != 'open':
        return jsonify({'error': 'Bidding is closed'}), 400

    data = request.get_json()
    amount = int(data.get('amount', 0))
    name = data.get('name', '').strip()
    email = data.get('email', '').strip()

    if amount < event['min_bid']:
        return jsonify({'error': f'Minimum bid is ${event["min_bid"]}'}), 400
    if not name or not email:
        return jsonify({'error': 'Name and email are required'}), 400

    try:
        intent = stripe.PaymentIntent.create(
            amount=amount * 100,  # cents
            currency='usd',
            capture_method='manual',  # authorize only — capture when winners are selected
            automatic_payment_methods={'enabled': True},
            metadata={
                'event_id': event_id,
                'bidder_name': name,
                'bidder_email': email,
            },
            description=f'Bid for Credit - {event["title"]} at {models.get_venue(event["venue_id"])["name"]}',
        )
        return jsonify({
            'client_secret': intent.client_secret,
            'payment_intent_id': intent.id,
        })
    except stripe.error.StripeError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/e/<event_id>/confirm-bid', methods=['POST'])
def confirm_bid(event_id):
    """After Stripe payment succeeds, record the bid."""
    event = models.get_event(event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404

    data = request.get_json()
    name = data.get('name', '').strip()
    email = data.get('email', '').strip()
    phone = data.get('phone', '').strip()
    amount = int(data.get('amount', 0))
    payment_intent_id = data.get('payment_intent_id', '')

    bid_id = models.place_bid(event_id, name, email, phone, amount, payment_intent_id)
    return jsonify({'bid_id': bid_id, 'redirect': f'/bid/{bid_id}'})


@app.route('/bid/<bid_id>')
def bid_status(bid_id):
    """Show bid status to the customer."""
    bid = models.get_bid(bid_id)
    if not bid:
        flash('Bid not found.', 'error')
        return redirect(url_for('index'))
    event = models.get_event(bid['event_id'])
    venue = models.get_venue(event['venue_id'])
    stats = models.get_bid_stats(bid['event_id'])
    return render_template('bid_status.html', bid=bid, event=event, venue=venue, stats=stats)


# ─── Admin: Dashboard ──────────────────────────────────────────

@app.route('/admin')
def admin_dashboard():
    venues = models.get_all_venues()
    return render_template('admin/dashboard.html', venues=venues)


@app.route('/admin/venue/new', methods=['GET', 'POST'])
def admin_new_venue():
    if request.method == 'POST':
        name = request.form['name']
        venue_type = request.form['type']
        description = request.form.get('description', '')
        location = request.form.get('location', '')
        venue_id = models.create_venue(name, venue_type, description, location)
        flash(f'Venue "{name}" created!', 'success')
        return redirect(url_for('admin_venue', venue_id=venue_id))
    return render_template('admin/new_venue.html')


@app.route('/admin/venue/<venue_id>')
def admin_venue(venue_id):
    venue = models.get_venue(venue_id)
    if not venue:
        flash('Venue not found.', 'error')
        return redirect(url_for('admin_dashboard'))
    events = models.get_events_for_venue(venue_id)
    return render_template('admin/venue.html', venue=venue, events=events)


@app.route('/admin/venue/<venue_id>/event/new', methods=['GET', 'POST'])
def admin_new_event(venue_id):
    venue = models.get_venue(venue_id)
    if not venue:
        flash('Venue not found.', 'error')
        return redirect(url_for('admin_dashboard'))
    if request.method == 'POST':
        title = request.form['title']
        date = request.form['date']
        time_slot = request.form.get('time_slot', '')
        spots = int(request.form.get('spots_available', 2))
        min_bid = int(request.form.get('min_bid', 50))
        bid_deadline = request.form.get('bid_deadline', '')
        event_id = models.create_event(venue_id, title, date, time_slot, spots, min_bid, bid_deadline)
        flash(f'Event "{title}" created!', 'success')
        return redirect(url_for('admin_event', event_id=event_id))
    return render_template('admin/new_event.html', venue=venue)


@app.route('/admin/event/<event_id>')
def admin_event(event_id):
    event = models.get_event(event_id)
    if not event:
        flash('Event not found.', 'error')
        return redirect(url_for('admin_dashboard'))
    venue = models.get_venue(event['venue_id'])
    bids = models.get_bids_for_event(event_id)
    winners, losers = models.get_winning_bids(event_id)

    # Generate QR code
    qr_url = f'{BASE_URL}/e/{event_id}'
    qr = qrcode.make(qr_url, box_size=8, border=2)
    buf = io.BytesIO()
    qr.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    return render_template('admin/event.html', event=event, venue=venue, bids=bids,
                           winners=winners, losers=losers, qr_b64=qr_b64, qr_url=qr_url)


@app.route('/admin/event/<event_id>/close', methods=['POST'])
def admin_close_event(event_id):
    """Close bidding and determine winners."""
    models.close_event(event_id)
    winners, losers = models.get_winning_bids(event_id)

    # Second-price logic: winners pay the (N+1)th highest bid or their own if fewer bids
    if losers:
        winning_price = losers[0]['amount']
    elif winners:
        winning_price = min(w['amount'] for w in winners)
    else:
        winning_price = 0

    for bid in winners:
        models.update_bid_status(bid['id'], 'won')
        # Capture the payment for winners
        if bid['stripe_payment_intent'] and stripe.api_key:
            try:
                stripe.PaymentIntent.capture(bid['stripe_payment_intent'])
            except stripe.error.StripeError:
                pass  # Log in production
    for bid in losers:
        models.update_bid_status(bid['id'], 'outbid')
        # Cancel/release hold for losers
        if bid['stripe_payment_intent'] and stripe.api_key:
            try:
                stripe.PaymentIntent.cancel(bid['stripe_payment_intent'])
            except stripe.error.StripeError:
                pass

    flash(f'Bidding closed! {len(winners)} winner(s) at ${winning_price} effective price.', 'success')
    return redirect(url_for('admin_event', event_id=event_id))


# ─── Init & Run ────────────────────────────────────────────────

with app.app_context():
    models.init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5001)
