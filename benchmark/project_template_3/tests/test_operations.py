"""Per-operation specification — passes once each stub is implemented correctly."""

import pytest

from app import accounts, catalog, cart, orders, payments, shipping, reviews, search


def test_validate_username():
    assert accounts.validate_username(*('alice_1',)) == True
    assert accounts.validate_username(*('1abc',)) == False
    assert accounts.validate_username(*('ab',)) == False
    assert accounts.validate_username(*('Alice',)) == False

def test_password_strength():
    assert accounts.password_strength(*('abc',)) == 'weak'
    assert accounts.password_strength(*('Password1',)) == 'medium'
    assert accounts.password_strength(*('Password1!',)) == 'strong'

def test_mask_email():
    assert accounts.mask_email(*('alice@shop.com',)) == 'a***e@shop.com'
    assert accounts.mask_email(*('bo@x.io',)) == 'b***o@x.io'
    with pytest.raises(ValueError):
        accounts.mask_email(*('not-an-email',))
    with pytest.raises(ValueError):
        accounts.mask_email(*('@x.com',))

def test_initials():
    assert accounts.initials(*('john ronald tolkien',)) == 'J.R.T.'
    assert accounts.initials(*('ada',)) == 'A.'
    with pytest.raises(ValueError):
        accounts.initials(*('   ',))

def test_signup_greeting():
    assert accounts.signup_greeting(*('ada',)) == 'Welcome, Ada!'
    assert accounts.signup_greeting(*('mary anne',)) == 'Welcome, Mary Anne!'

def test_is_adult():
    assert accounts.is_adult(*(2000, 2026)) == True
    assert accounts.is_adult(*(2010, 2026)) == False
    assert accounts.is_adult(*(2008, 2026)) == True
    with pytest.raises(ValueError):
        accounts.is_adult(*(2030, 2026))

def test_login_throttle_delay():
    assert accounts.login_throttle_delay(*(2,)) == 0
    assert accounts.login_throttle_delay(*(3,)) == 1
    assert accounts.login_throttle_delay(*(5,)) == 4
    assert accounts.login_throttle_delay(*(10,)) == 60
    with pytest.raises(ValueError):
        accounts.login_throttle_delay(*(-1,))

def test_slugify():
    assert catalog.slugify(*('Hello, World!',)) == 'hello-world'
    assert catalog.slugify(*('  Django 5 -- Release  ',)) == 'django-5-release'

def test_format_price():
    assert catalog.format_price(*(1234,)) == '$12.34'
    assert catalog.format_price(*(5,)) == '$0.05'
    assert catalog.format_price(*(0,)) == '$0.00'
    with pytest.raises(ValueError):
        catalog.format_price(*(-1,))

def test_apply_discount():
    assert catalog.apply_discount(*(1000, 25)) == 750
    assert catalog.apply_discount(*(999, 10)) == 899
    assert catalog.apply_discount(*(500, 0)) == 500
    with pytest.raises(ValueError):
        catalog.apply_discount(*(100, 101))
    with pytest.raises(ValueError):
        catalog.apply_discount(*(100, -1))

def test_in_stock():
    assert catalog.in_stock(*(5, 3)) == True
    assert catalog.in_stock(*(2, 3)) == False
    with pytest.raises(ValueError):
        catalog.in_stock(*(5, 0))

def test_sku():
    assert catalog.sku(*('shoes', 42)) == 'SHO-00042'
    assert catalog.sku(*('tv', 7)) == 'TV-00007'
    with pytest.raises(ValueError):
        catalog.sku(*('', 1))
    with pytest.raises(ValueError):
        catalog.sku(*('x', 0))

def test_star_bar():
    assert catalog.star_bar(*(3,)) == '★★★☆☆'
    assert catalog.star_bar(*(0,)) == '☆☆☆☆☆'
    with pytest.raises(ValueError):
        catalog.star_bar(*(6,))
    with pytest.raises(ValueError):
        catalog.star_bar(*(-1,))

def test_list_page():
    assert catalog.list_page(*([10, 20, 30, 40, 50], 2, 2)) == [30, 40]
    assert catalog.list_page(*([1, 2, 3], 1, 5)) == [1, 2, 3]
    with pytest.raises(ValueError):
        catalog.list_page(*([1], 0, 2))
    with pytest.raises(ValueError):
        catalog.list_page(*([1], 1, 0))

def test_bulk_price():
    assert catalog.bulk_price(*(1000, 5)) == 1000
    assert catalog.bulk_price(*(1000, 10)) == 900
    assert catalog.bulk_price(*(1000, 50)) == 800
    with pytest.raises(ValueError):
        catalog.bulk_price(*(1000, 0))

def test_cart_total():
    assert cart.cart_total(*([100, 250],)) == 350
    assert cart.cart_total(*([],)) == 0
    with pytest.raises(ValueError):
        cart.cart_total(*([100, -5],))

def test_add_item():
    assert cart.add_item(*(['a'], 'b', 3)) == ['a', 'b']
    assert cart.add_item(*([], 'x', 1)) == ['x']
    with pytest.raises(ValueError):
        cart.add_item(*(['a', 'b'], 'c', 2))

def test_item_counts():
    assert cart.item_counts(*(['a', 'b', 'a'],)) == {'a': 2, 'b': 1}
    assert cart.item_counts(*([],)) == {}

def test_apply_coupon():
    assert cart.apply_coupon(*(1000, 'SAVE10')) == 900
    assert cart.apply_coupon(*(1000, 'SAVE25')) == 750
    with pytest.raises(ValueError):
        cart.apply_coupon(*(1000, 'HELLO'))

def test_free_shipping_eligible():
    assert cart.free_shipping_eligible(*(5000, 5000)) == True
    assert cart.free_shipping_eligible(*(4999, 5000)) == False

def test_remove_item():
    assert cart.remove_item(*(['a', 'b', 'a'], 'a')) == ['b', 'a']
    with pytest.raises(ValueError):
        cart.remove_item(*(['b'], 'x'))

def test_quantity_update():
    assert cart.quantity_update(*({'a': 1}, 'b', 3)) == {'a': 1, 'b': 3}
    assert cart.quantity_update(*({'a': 2, 'b': 1}, 'a', 0)) == {'b': 1}
    with pytest.raises(ValueError):
        cart.quantity_update(*({}, 'a', -1))

def test_order_number():
    assert orders.order_number(*(42, 2026)) == 'ORD-2026-000042'
    with pytest.raises(ValueError):
        orders.order_number(*(0, 2026))

def test_next_status():
    assert orders.next_status(*('placed',)) == 'paid'
    assert orders.next_status(*('shipped',)) == 'delivered'
    with pytest.raises(ValueError):
        orders.next_status(*('delivered',))
    with pytest.raises(ValueError):
        orders.next_status(*('weird',))

def test_order_total():
    assert orders.order_total(*(10000, 875, 500)) == 11375
    assert orders.order_total(*(0, 875, 0)) == 0
    with pytest.raises(ValueError):
        orders.order_total(*(10000, 875, -1))

def test_confirmation_line():
    assert orders.confirmation_line(*('ORD-2026-000042', 11375)) == 'Order ORD-2026-000042 confirmed - total $113.75'

def test_estimated_delivery():
    assert orders.estimated_delivery(*(5, 3)) == 1
    assert orders.estimated_delivery(*(0, 7)) == 0
    with pytest.raises(ValueError):
        orders.estimated_delivery(*(7, 1))
    with pytest.raises(ValueError):
        orders.estimated_delivery(*(-1, 1))

def test_cancel_allowed():
    assert orders.cancel_allowed(*('placed',)) == True
    assert orders.cancel_allowed(*('shipped',)) == False
    with pytest.raises(ValueError):
        orders.cancel_allowed(*('weird',))

def test_split_shipments():
    assert orders.split_shipments(*([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert orders.split_shipments(*([], 3)) == []
    with pytest.raises(ValueError):
        orders.split_shipments(*([1], 0))

def test_refund_amount():
    assert orders.refund_amount(*(1000, 10, 30)) == 1000
    assert orders.refund_amount(*(1000, 45, 30)) == 500
    assert orders.refund_amount(*(999, 45, 30)) == 499
    assert orders.refund_amount(*(1000, 61, 30)) == 0
    with pytest.raises(ValueError):
        orders.refund_amount(*(1000, 1, 0))

def test_luhn_valid():
    assert payments.luhn_valid(*('79927398713',)) == True
    assert payments.luhn_valid(*('79927398710',)) == False
    with pytest.raises(ValueError):
        payments.luhn_valid(*('79a',))
    with pytest.raises(ValueError):
        payments.luhn_valid(*('',))

def test_mask_card():
    assert payments.mask_card(*('4111111111111111',)) == '**** **** **** 1111'
    assert payments.mask_card(*('12345',)) == '**** **** **** 2345'
    with pytest.raises(ValueError):
        payments.mask_card(*('123',))

def test_processing_fee():
    assert payments.processing_fee(*(10000, 290)) == 290
    assert payments.processing_fee(*(999, 290)) == 28
    with pytest.raises(ValueError):
        payments.processing_fee(*(-1, 290))

def test_split_evenly():
    assert payments.split_evenly(*(10, 3)) == [4, 3, 3]
    assert payments.split_evenly(*(9, 3)) == [3, 3, 3]
    with pytest.raises(ValueError):
        payments.split_evenly(*(5, 0))

def test_currency_to_cents():
    assert payments.currency_to_cents(*('$12.34',)) == 1234
    assert payments.currency_to_cents(*('$0.05',)) == 5
    with pytest.raises(ValueError):
        payments.currency_to_cents(*('12.34',))
    with pytest.raises(ValueError):
        payments.currency_to_cents(*('$12.3',))
    with pytest.raises(ValueError):
        payments.currency_to_cents(*('$12',))

def test_is_expired():
    assert payments.is_expired(*(6, 2026, 7, 2026)) == True
    assert payments.is_expired(*(7, 2026, 7, 2026)) == False
    assert payments.is_expired(*(1, 2027, 12, 2026)) == False
    with pytest.raises(ValueError):
        payments.is_expired(*(13, 2026, 1, 2026))

def test_receipt_id():
    assert payments.receipt_id(*('ORD-2026-000042', 3)) == 'ORD-2026-000042/R03'
    with pytest.raises(ValueError):
        payments.receipt_id(*('X', 0))

def test_shipping_cost():
    assert shipping.shipping_cost(*(1500, 1)) == 700
    assert shipping.shipping_cost(*(1000, 2)) == 1000
    assert shipping.shipping_cost(*(1, 3)) == 1500
    with pytest.raises(ValueError):
        shipping.shipping_cost(*(0, 1))
    with pytest.raises(ValueError):
        shipping.shipping_cost(*(100, 9))

def test_normalize_postcode():
    assert shipping.normalize_postcode(*(' se1 9gf ',)) == 'SE19GF'
    assert shipping.normalize_postcode(*('10001',)) == '10001'
    with pytest.raises(ValueError):
        shipping.normalize_postcode(*('',))
    with pytest.raises(ValueError):
        shipping.normalize_postcode(*('s!1',))

def test_address_label():
    assert shipping.address_label(*('Ada Lovelace', '12 Byte St', 'London', 'SE1')) == 'ADA LOVELACE\n12 Byte St\nLondon SE1'
    with pytest.raises(ValueError):
        shipping.address_label(*('', 's', 'c', 'z'))

def test_delivery_window():
    assert shipping.delivery_window(*(6, True)) == 3
    assert shipping.delivery_window(*(1, True)) == 1
    assert shipping.delivery_window(*(5, False)) == 5
    with pytest.raises(ValueError):
        shipping.delivery_window(*(0, False))

def test_tracking_valid():
    assert shipping.tracking_valid(*('AB123456789',)) == True
    assert shipping.tracking_valid(*('ab123456789',)) == False
    assert shipping.tracking_valid(*('AB12345678',)) == False
    with pytest.raises(ValueError):
        shipping.tracking_valid(*('',))

def test_zone_for_country():
    assert shipping.zone_for_country(*('US',)) == 1
    assert shipping.zone_for_country(*('uk',)) == 2
    assert shipping.zone_for_country(*('jp',)) == 3
    with pytest.raises(ValueError):
        shipping.zone_for_country(*('',))

def test_oversize_surcharge():
    assert shipping.oversize_surcharge(*(25000, 20000, 150)) == 750
    assert shipping.oversize_surcharge(*(20000, 20000, 150)) == 0
    assert shipping.oversize_surcharge(*(20001, 20000, 150)) == 150
    with pytest.raises(ValueError):
        shipping.oversize_surcharge(*(100, 0, 10))

def test_average_rating():
    assert reviews.average_rating(*([4, 5],)) == 4.5
    assert reviews.average_rating(*([3, 4, 4],)) == 3.7
    assert reviews.average_rating(*([5],)) == 5.0
    with pytest.raises(ValueError):
        reviews.average_rating(*([],))
    with pytest.raises(ValueError):
        reviews.average_rating(*([0],))
    with pytest.raises(ValueError):
        reviews.average_rating(*([6],))

def test_star_histogram():
    assert reviews.star_histogram(*([5, 5, 3],)) == {1: 0, 2: 0, 3: 1, 4: 0, 5: 2}
    with pytest.raises(ValueError):
        reviews.star_histogram(*([0],))

def test_contains_profanity():
    assert reviews.contains_profanity(*('This is Darn good', ['darn'])) == True
    assert reviews.contains_profanity(*('clean text', ['darn'])) == False
    assert reviews.contains_profanity(*('scandarnous', ['darn'])) == False

def test_helpfulness():
    assert reviews.helpfulness(*(3, 1)) == 75
    assert reviews.helpfulness(*(0, 0)) == 0
    assert reviews.helpfulness(*(1, 2)) == 33
    with pytest.raises(ValueError):
        reviews.helpfulness(*(-1, 0))

def test_truncate_review():
    assert reviews.truncate_review(*('great product would buy again', 12)) == 'great produ…'
    assert reviews.truncate_review(*('nice', 10)) == 'nice'
    with pytest.raises(ValueError):
        reviews.truncate_review(*('x', 0))

def test_verified_badge():
    assert reviews.verified_badge(*(True, 4)) == 'Verified ★4'
    assert reviews.verified_badge(*(False, 5)) == '★5'
    with pytest.raises(ValueError):
        reviews.verified_badge(*(True, 0))
    with pytest.raises(ValueError):
        reviews.verified_badge(*(False, 6))

def test_sort_reviews():
    assert reviews.sort_reviews(*([(10, 'b'), (50, 'a'), (10, 'a')],)) == [(50, 'a'), (10, 'a'), (10, 'b')]

def test_tokenize():
    assert search.tokenize(*('Hello, World HELLO',)) == ['hello', 'world', 'hello']
    assert search.tokenize(*('',)) == []

def test_match_score():
    assert search.match_score(*(['red', 'shoe'], ['shoe', 'red', 'laces'])) == 2
    assert search.match_score(*(['red'], ['blue'])) == 0

def test_highlight():
    assert search.highlight(*('The Cat sat on a cat', 'cat')) == 'The [Cat] sat on a [cat]'
    assert search.highlight(*('no match here', 'zz')) == 'no match here'
    with pytest.raises(ValueError):
        search.highlight(*('text', ''))

def test_suggest():
    assert search.suggest(*('ca', ['cart', 'cat', 'castle', 'carbon', 'cave', 'cap'])) == ['cap', 'carbon', 'cart', 'castle', 'cat']
    assert search.suggest(*('zz', ['cart'])) == []

def test_page_count():
    assert search.page_count(*(45, 10)) == 5
    assert search.page_count(*(40, 10)) == 4
    assert search.page_count(*(0, 10)) == 0
    with pytest.raises(ValueError):
        search.page_count(*(10, 0))

def test_filter_by_price():
    assert search.filter_by_price(*([('a', 5), ('b', 15), ('c', 10)], 5, 10)) == [('a', 5), ('c', 10)]
    with pytest.raises(ValueError):
        search.filter_by_price(*([], 10, 5))

def test_query_time_bucket():
    assert search.query_time_bucket(*(50,)) == 'fast'
    assert search.query_time_bucket(*(100,)) == 'ok'
    assert search.query_time_bucket(*(999,)) == 'slow'
    with pytest.raises(ValueError):
        search.query_time_bucket(*(-1,))
