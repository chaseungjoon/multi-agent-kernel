"""Shared-table specification — every feature registration must land intact.

This is the oracle for the contended files: a registration dropped in a bad
merge (or a lost node-store update) fails here even when the feature's own
function tests pass.
"""

from app import errors, events, routes, settings


def test_route_registered_post_accounts_validate():
    assert 'POST /accounts/validate' in routes.ROUTES


def test_route_dispatches_post_accounts_validate():
    assert routes.dispatch('POST /accounts/validate', *('alice_1',)) == True

def test_route_registered_get_catalog_slug():
    assert 'GET /catalog/slug' in routes.ROUTES


def test_route_dispatches_get_catalog_slug():
    assert routes.dispatch('GET /catalog/slug', *('Hello, World!',)) == 'hello-world'

def test_route_registered_get_catalog_price():
    assert 'GET /catalog/price' in routes.ROUTES


def test_route_dispatches_get_catalog_price():
    assert routes.dispatch('GET /catalog/price', *(1234,)) == '$12.34'

def test_route_registered_get_catalog_page():
    assert 'GET /catalog/page' in routes.ROUTES


def test_route_dispatches_get_catalog_page():
    assert routes.dispatch('GET /catalog/page', *([10, 20, 30, 40, 50], 2, 2)) == [30, 40]

def test_route_registered_get_cart_total():
    assert 'GET /cart/total' in routes.ROUTES


def test_route_dispatches_get_cart_total():
    assert routes.dispatch('GET /cart/total', *([100, 250],)) == 350

def test_route_registered_post_cart_items():
    assert 'POST /cart/items' in routes.ROUTES


def test_route_dispatches_post_cart_items():
    assert routes.dispatch('POST /cart/items', *(['a'], 'b', 3)) == ['a', 'b']

def test_route_registered_post_cart_quantity():
    assert 'POST /cart/quantity' in routes.ROUTES


def test_route_dispatches_post_cart_quantity():
    assert routes.dispatch('POST /cart/quantity', *({'a': 1}, 'b', 3)) == {'a': 1, 'b': 3}

def test_route_registered_post_orders():
    assert 'POST /orders' in routes.ROUTES


def test_route_dispatches_post_orders():
    assert routes.dispatch('POST /orders', *(42, 2026)) == 'ORD-2026-000042'

def test_route_registered_get_orders_total():
    assert 'GET /orders/total' in routes.ROUTES


def test_route_dispatches_get_orders_total():
    assert routes.dispatch('GET /orders/total', *(10000, 875, 500)) == 11375

def test_route_registered_post_payments_validate():
    assert 'POST /payments/validate' in routes.ROUTES


def test_route_dispatches_post_payments_validate():
    assert routes.dispatch('POST /payments/validate', *('79927398713',)) == True

def test_route_registered_get_payments_split():
    assert 'GET /payments/split' in routes.ROUTES


def test_route_dispatches_get_payments_split():
    assert routes.dispatch('GET /payments/split', *(10, 3)) == [4, 3, 3]

def test_route_registered_post_payments_parse():
    assert 'POST /payments/parse' in routes.ROUTES


def test_route_dispatches_post_payments_parse():
    assert routes.dispatch('POST /payments/parse', *('$12.34',)) == 1234

def test_route_registered_get_shipping_cost():
    assert 'GET /shipping/cost' in routes.ROUTES


def test_route_dispatches_get_shipping_cost():
    assert routes.dispatch('GET /shipping/cost', *(1500, 1)) == 700

def test_route_registered_post_shipping_label():
    assert 'POST /shipping/label' in routes.ROUTES


def test_route_dispatches_post_shipping_label():
    assert routes.dispatch('POST /shipping/label', *('Ada Lovelace', '12 Byte St', 'London', 'SE1')) == 'ADA LOVELACE\n12 Byte St\nLondon SE1'

def test_route_registered_get_reviews_average():
    assert 'GET /reviews/average' in routes.ROUTES


def test_route_dispatches_get_reviews_average():
    assert routes.dispatch('GET /reviews/average', *([4, 5],)) == 4.5

def test_route_registered_get_reviews_preview():
    assert 'GET /reviews/preview' in routes.ROUTES


def test_route_dispatches_get_reviews_preview():
    assert routes.dispatch('GET /reviews/preview', *('great product would buy again', 12)) == 'great produ…'

def test_route_registered_get_search():
    assert 'GET /search' in routes.ROUTES


def test_route_dispatches_get_search():
    assert routes.dispatch('GET /search', *('Hello, World HELLO',)) == ['hello', 'world', 'hello']

def test_route_registered_get_search_highlight():
    assert 'GET /search/highlight' in routes.ROUTES


def test_route_dispatches_get_search_highlight():
    assert routes.dispatch('GET /search/highlight', *('The Cat sat on a cat', 'cat')) == 'The [Cat] sat on a [cat]'

def test_route_registered_get_search_filter():
    assert 'GET /search/filter' in routes.ROUTES


def test_route_dispatches_get_search_filter():
    assert routes.dispatch('GET /search/filter', *([('a', 5), ('b', 15), ('c', 10)], 5, 10)) == [('a', 5), ('c', 10)]

def test_event_registered_account_created():
    assert 'account.created' in events.HANDLERS


def test_event_emits_account_created():
    assert events.emit('account.created', *('ada',)) == 'Welcome, Ada!'

def test_event_registered_order_status_changed():
    assert 'order.status_changed' in events.HANDLERS


def test_event_emits_order_status_changed():
    assert events.emit('order.status_changed', *('placed',)) == 'paid'

def test_event_registered_order_placed():
    assert 'order.placed' in events.HANDLERS


def test_event_emits_order_placed():
    assert events.emit('order.placed', *('ORD-2026-000042', 11375)) == 'Order ORD-2026-000042 confirmed - total $113.75'

def test_event_registered_payment_captured():
    assert 'payment.captured' in events.HANDLERS


def test_event_emits_payment_captured():
    assert events.emit('payment.captured', *('ORD-2026-000042', 3)) == 'ORD-2026-000042/R03'

def test_event_registered_review_submitted():
    assert 'review.submitted' in events.HANDLERS


def test_event_emits_review_submitted():
    assert events.emit('review.submitted', *(True, 4)) == 'Verified ★4'

def test_event_registered_search_performed():
    assert 'search.performed' in events.HANDLERS


def test_event_emits_search_performed():
    assert events.emit('search.performed', *(50,)) == 'fast'

def test_error_registered_accounts_invalid_email():
    assert 'accounts.invalid_email' in errors.ERRORS


def test_error_message_accounts_invalid_email():
    assert errors.message_for('accounts.invalid_email') == 'Email address is not valid'

def test_error_registered_accounts_locked():
    assert 'accounts.locked' in errors.ERRORS


def test_error_message_accounts_locked():
    assert errors.message_for('accounts.locked') == 'Too many failed sign-in attempts'

def test_error_registered_catalog_bad_discount():
    assert 'catalog.bad_discount' in errors.ERRORS


def test_error_message_catalog_bad_discount():
    assert errors.message_for('catalog.bad_discount') == 'Discount must be between 0 and 100 percent'

def test_error_registered_cart_limit_exceeded():
    assert 'cart.limit_exceeded' in errors.ERRORS


def test_error_message_cart_limit_exceeded():
    assert errors.message_for('cart.limit_exceeded') == 'Cart cannot exceed the maximum number of items'

def test_error_registered_cart_bad_coupon():
    assert 'cart.bad_coupon' in errors.ERRORS


def test_error_message_cart_bad_coupon():
    assert errors.message_for('cart.bad_coupon') == 'Coupon code is not recognized'

def test_error_registered_orders_cancel_denied():
    assert 'orders.cancel_denied' in errors.ERRORS


def test_error_message_orders_cancel_denied():
    assert errors.message_for('orders.cancel_denied') == 'Order can no longer be cancelled'

def test_error_registered_payments_bad_amount():
    assert 'payments.bad_amount' in errors.ERRORS


def test_error_message_payments_bad_amount():
    assert errors.message_for('payments.bad_amount') == 'Amount must look like $D.DD'

def test_error_registered_shipping_oversize():
    assert 'shipping.oversize' in errors.ERRORS


def test_error_message_shipping_oversize():
    assert errors.message_for('shipping.oversize') == 'Package exceeds the standard weight limit'

def test_error_registered_reviews_rejected():
    assert 'reviews.rejected' in errors.ERRORS


def test_error_message_reviews_rejected():
    assert errors.message_for('reviews.rejected') == 'Review contains prohibited language'

def test_setting_registered_security_min_password_length():
    assert 'security.min_password_length' in settings.DEFAULTS


def test_setting_value_security_min_password_length():
    assert settings.get('security.min_password_length') == 8

def test_setting_registered_accounts_min_age():
    assert 'accounts.min_age' in settings.DEFAULTS


def test_setting_value_accounts_min_age():
    assert settings.get('accounts.min_age') == 18

def test_setting_registered_catalog_sku_width():
    assert 'catalog.sku_width' in settings.DEFAULTS


def test_setting_value_catalog_sku_width():
    assert settings.get('catalog.sku_width') == 5

def test_setting_registered_catalog_bulk_threshold():
    assert 'catalog.bulk_threshold' in settings.DEFAULTS


def test_setting_value_catalog_bulk_threshold():
    assert settings.get('catalog.bulk_threshold') == 10

def test_setting_registered_cart_free_shipping_cents():
    assert 'cart.free_shipping_cents' in settings.DEFAULTS


def test_setting_value_cart_free_shipping_cents():
    assert settings.get('cart.free_shipping_cents') == 5000

def test_setting_registered_orders_tax_basis_points():
    assert 'orders.tax_basis_points' in settings.DEFAULTS


def test_setting_value_orders_tax_basis_points():
    assert settings.get('orders.tax_basis_points') == 875

def test_setting_registered_orders_refund_window_days():
    assert 'orders.refund_window_days' in settings.DEFAULTS


def test_setting_value_orders_refund_window_days():
    assert settings.get('orders.refund_window_days') == 30

def test_setting_registered_payments_fee_basis_points():
    assert 'payments.fee_basis_points' in settings.DEFAULTS


def test_setting_value_payments_fee_basis_points():
    assert settings.get('payments.fee_basis_points') == 290

def test_setting_registered_shipping_express_divisor():
    assert 'shipping.express_divisor' in settings.DEFAULTS


def test_setting_value_shipping_express_divisor():
    assert settings.get('shipping.express_divisor') == 2

def test_setting_registered_shipping_default_zone():
    assert 'shipping.default_zone' in settings.DEFAULTS


def test_setting_value_shipping_default_zone():
    assert settings.get('shipping.default_zone') == 3

def test_setting_registered_search_max_suggestions():
    assert 'search.max_suggestions' in settings.DEFAULTS


def test_setting_value_search_max_suggestions():
    assert settings.get('search.max_suggestions') == 5
