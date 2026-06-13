"""Per-operation specification — passes once each stub is implemented correctly."""

import pytest

from toolkit import strkit, numkit, seqkit, dictkit, datekit, mathkit, parsekit, setkit, codekit


def test_camel_to_snake():
    assert strkit.camel_to_snake(*('fooBarBaz',)) == 'foo_bar_baz'
    assert strkit.camel_to_snake(*('plain',)) == 'plain'

def test_snake_to_camel():
    assert strkit.snake_to_camel(*('foo_bar_baz',)) == 'fooBarBaz'
    assert strkit.snake_to_camel(*('single',)) == 'single'

def test_truncate():
    assert strkit.truncate(*('hello world', 8)) == 'hello...'
    assert strkit.truncate(*('hi', 8)) == 'hi'
    with pytest.raises(ValueError):
        strkit.truncate(*('x', 2))

def test_word_wrap():
    assert strkit.word_wrap(*('a bb ccc dddd', 5)) == ['a bb', 'ccc', 'dddd']
    with pytest.raises(ValueError):
        strkit.word_wrap(*('hi', 0))

def test_levenshtein():
    assert strkit.levenshtein(*('kitten', 'sitting')) == 3
    assert strkit.levenshtein(*('abc', 'abc')) == 0

def test_longest_common_prefix():
    assert strkit.longest_common_prefix(*(['flower', 'flow', 'flight'],)) == 'fl'
    assert strkit.longest_common_prefix(*(['a', 'b'],)) == ''

def test_is_anagram():
    assert strkit.is_anagram(*('Listen', 'Silent')) == True
    assert strkit.is_anagram(*('foo', 'bar')) == False

def test_title_case():
    assert strkit.title_case(*('hello WORLD',)) == 'Hello World'

def test_count_substring():
    assert strkit.count_substring(*('ababab', 'ab')) == 3
    assert strkit.count_substring(*('aaa', 'aa')) == 1

def test_rot13():
    assert strkit.rot13(*('Hello',)) == 'Uryyb'

def test_is_perfect_square():
    assert numkit.is_perfect_square(*(16,)) == True
    assert numkit.is_perfect_square(*(15,)) == False
    assert numkit.is_perfect_square(*(0,)) == True

def test_prime_factors():
    assert numkit.prime_factors(*(12,)) == [2, 2, 3]
    assert numkit.prime_factors(*(13,)) == [13]
    assert numkit.prime_factors(*(1,)) == []

def test_nth_prime():
    assert numkit.nth_prime(*(1,)) == 2
    assert numkit.nth_prime(*(5,)) == 11
    with pytest.raises(ValueError):
        numkit.nth_prime(*(0,))

def test_collatz_steps():
    assert numkit.collatz_steps(*(1,)) == 0
    assert numkit.collatz_steps(*(6,)) == 8
    with pytest.raises(ValueError):
        numkit.collatz_steps(*(0,))

def test_gcd_many():
    assert numkit.gcd_many(*([12, 18, 24],)) == 6
    assert numkit.gcd_many(*([7],)) == 7
    with pytest.raises(ValueError):
        numkit.gcd_many(*([],))

def test_lcm_many():
    assert numkit.lcm_many(*([4, 6, 8],)) == 24
    assert numkit.lcm_many(*([5],)) == 5
    with pytest.raises(ValueError):
        numkit.lcm_many(*([],))

def test_base_convert():
    assert numkit.base_convert(*(255, 16)) == 'ff'
    assert numkit.base_convert(*(10, 2)) == '1010'
    with pytest.raises(ValueError):
        numkit.base_convert(*(5, 1))

def test_clamp():
    assert numkit.clamp(*(5, 0, 10)) == 5
    assert numkit.clamp(*(-3, 0, 10)) == 0
    with pytest.raises(ValueError):
        numkit.clamp(*(1, 5, 0))

def test_divisors():
    assert numkit.divisors(*(12,)) == [1, 2, 3, 4, 6, 12]
    assert numkit.divisors(*(7,)) == [1, 7]
    with pytest.raises(ValueError):
        numkit.divisors(*(0,))

def test_mean():
    assert numkit.mean(*([1, 2, 3, 4],)) == 2.5
    assert numkit.mean(*([5],)) == 5.0
    with pytest.raises(ValueError):
        numkit.mean(*([],))

def test_windowed():
    assert seqkit.windowed(*([1, 2, 3, 4], 2)) == [[1, 2], [2, 3], [3, 4]]
    assert seqkit.windowed(*([1], 2)) == []
    with pytest.raises(ValueError):
        seqkit.windowed(*([1, 2], 0))

def test_chunk():
    assert seqkit.chunk(*([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    with pytest.raises(ValueError):
        seqkit.chunk(*([1], 0))

def test_flatten_deep():
    assert seqkit.flatten_deep(*([1, [2, [3, 4]], 5],)) == [1, 2, 3, 4, 5]

def test_rotate():
    assert seqkit.rotate(*([1, 2, 3, 4, 5], 2)) == [3, 4, 5, 1, 2]
    assert seqkit.rotate(*([1, 2, 3], -1)) == [3, 1, 2]

def test_pairwise():
    assert seqkit.pairwise(*([1, 2, 3],)) == [(1, 2), (2, 3)]
    assert seqkit.pairwise(*([1],)) == []

def test_dedupe():
    assert seqkit.dedupe(*([1, 1, 2, 3, 2],)) == [1, 2, 3]

def test_frequencies():
    assert seqkit.frequencies(*(['a', 'b', 'a'],)) == {'a': 2, 'b': 1}

def test_partition_even_odd():
    assert seqkit.partition_even_odd(*([1, 2, 3, 4],)) == ([2, 4], [1, 3])

def test_interleave():
    assert seqkit.interleave(*([1, 3], [2, 4])) == [1, 2, 3, 4]
    assert seqkit.interleave(*([1], [2, 3, 4])) == [1, 2, 3, 4]

def test_run_length():
    assert seqkit.run_length(*(['a', 'a', 'b'],)) == [('a', 2), ('b', 1)]

def test_invert():
    assert dictkit.invert(*({'a': 1, 'b': 2},)) == {1: 'a', 2: 'b'}

def test_merge():
    assert dictkit.merge(*({'a': 1}, {'b': 2, 'a': 3})) == {'a': 3, 'b': 2}

def test_pick():
    assert dictkit.pick(*({'a': 1, 'b': 2, 'c': 3}, ['a', 'c', 'x'])) == {'a': 1, 'c': 3}

def test_omit():
    assert dictkit.omit(*({'a': 1, 'b': 2, 'c': 3}, ['b'])) == {'a': 1, 'c': 3}

def test_get_in():
    assert dictkit.get_in(*({'a': {'b': {'c': 5}}}, ['a', 'b', 'c'])) == 5
    assert dictkit.get_in(*({'a': 1}, ['x'])) == None

def test_key_of_max():
    assert dictkit.key_of_max(*({'a': 1, 'b': 9, 'c': 3},)) == 'b'
    with pytest.raises(ValueError):
        dictkit.key_of_max(*({},))

def test_count_values():
    assert dictkit.count_values(*({'a': 1, 'b': 1, 'c': 2},)) == {1: 2, 2: 1}

def test_deep_keys():
    assert dictkit.deep_keys(*({'a': 1, 'b': {'c': 2, 'd': 3}},)) == ['a', 'b', 'c', 'd']

def test_zip_dict():
    assert dictkit.zip_dict(*(['a', 'b'], [1, 2])) == {'a': 1, 'b': 2}

def test_max_value():
    assert dictkit.max_value(*({'a': 1, 'b': 5},)) == 5
    with pytest.raises(ValueError):
        dictkit.max_value(*({},))

def test_is_leap_year():
    assert datekit.is_leap_year(*(2000,)) == True
    assert datekit.is_leap_year(*(1900,)) == False
    assert datekit.is_leap_year(*(2024,)) == True

def test_days_in_month():
    assert datekit.days_in_month(*(2024, 2)) == 29
    assert datekit.days_in_month(*(2023, 2)) == 28
    assert datekit.days_in_month(*(2024, 4)) == 30
    with pytest.raises(ValueError):
        datekit.days_in_month(*(2024, 13))

def test_day_of_year():
    assert datekit.day_of_year(*(2024, 3, 1)) == 61
    assert datekit.day_of_year(*(2023, 1, 1)) == 1

def test_weekday():
    assert datekit.weekday(*(2024, 1, 1)) == 1
    assert datekit.weekday(*(2000, 1, 1)) == 6

def test_is_valid_date():
    assert datekit.is_valid_date(*(2024, 2, 29)) == True
    assert datekit.is_valid_date(*(2023, 2, 29)) == False
    assert datekit.is_valid_date(*(2024, 13, 1)) == False

def test_quarter_of():
    assert datekit.quarter_of(*(1,)) == 1
    assert datekit.quarter_of(*(4,)) == 2
    assert datekit.quarter_of(*(12,)) == 4
    with pytest.raises(ValueError):
        datekit.quarter_of(*(0,))

def test_days_until_year_end():
    assert datekit.days_until_year_end(*(2024, 12, 31)) == 0
    assert datekit.days_until_year_end(*(2023, 1, 1)) == 364

def test_month_name():
    assert datekit.month_name(*(1,)) == 'January'
    assert datekit.month_name(*(12,)) == 'December'
    with pytest.raises(ValueError):
        datekit.month_name(*(0,))

def test_season():
    assert datekit.season(*(1,)) == 'winter'
    assert datekit.season(*(4,)) == 'spring'
    assert datekit.season(*(7,)) == 'summer'
    assert datekit.season(*(10,)) == 'autumn'
    with pytest.raises(ValueError):
        datekit.season(*(13,))

def test_age_in_years():
    assert datekit.age_in_years(*(2000, 6, 15, 2024, 6, 14)) == 23
    assert datekit.age_in_years(*(2000, 6, 15, 2024, 6, 15)) == 24

def test_fib():
    assert mathkit.fib(*(10,)) == 55
    assert mathkit.fib(*(0,)) == 0
    with pytest.raises(ValueError):
        mathkit.fib(*(-1,))

def test_factorial():
    assert mathkit.factorial(*(5,)) == 120
    assert mathkit.factorial(*(0,)) == 1
    with pytest.raises(ValueError):
        mathkit.factorial(*(-1,))

def test_binomial():
    assert mathkit.binomial(*(5, 2)) == 10
    assert mathkit.binomial(*(6, 0)) == 1

def test_sum_digits():
    assert mathkit.sum_digits(*(1234,)) == 10
    assert mathkit.sum_digits(*(-9,)) == 9

def test_digital_root():
    assert mathkit.digital_root(*(9875,)) == 2
    assert mathkit.digital_root(*(0,)) == 0

def test_is_armstrong():
    assert mathkit.is_armstrong(*(153,)) == True
    assert mathkit.is_armstrong(*(154,)) == False

def test_triangular():
    assert mathkit.triangular(*(5,)) == 15
    assert mathkit.triangular(*(0,)) == 0
    with pytest.raises(ValueError):
        mathkit.triangular(*(-1,))

def test_sum_primes_below():
    assert mathkit.sum_primes_below(*(10,)) == 17
    assert mathkit.sum_primes_below(*(2,)) == 0

def test_power_mod():
    assert mathkit.power_mod(*(2, 10, 1000)) == 24
    assert mathkit.power_mod(*(3, 0, 7)) == 1
    with pytest.raises(ValueError):
        mathkit.power_mod(*(2, 2, 0))

def test_is_prime():
    assert mathkit.is_prime(*(7,)) == True
    assert mathkit.is_prime(*(9,)) == False
    assert mathkit.is_prime(*(2,)) == True

def test_parse_csv_line():
    assert parsekit.parse_csv_line(*('a,b,c',)) == ['a', 'b', 'c']
    assert parsekit.parse_csv_line(*('',)) == ['']

def test_parse_query_string():
    assert parsekit.parse_query_string(*('a=1&b=2',)) == {'a': '1', 'b': '2'}
    assert parsekit.parse_query_string(*('x',)) == {'x': ''}

def test_tokenize_words():
    assert parsekit.tokenize_words(*('Hello, World!',)) == ['hello', 'world']

def test_parse_kv():
    assert parsekit.parse_kv(*('a:1; b:2',)) == {'a': '1', 'b': '2'}

def test_balanced_brackets():
    assert parsekit.balanced_brackets(*('([]{})',)) == True
    assert parsekit.balanced_brackets(*('([)]',)) == False
    assert parsekit.balanced_brackets(*('(',)) == False

def test_roman_to_int():
    assert parsekit.roman_to_int(*('IV',)) == 4
    assert parsekit.roman_to_int(*('MCMXCIV',)) == 1994

def test_int_to_roman():
    assert parsekit.int_to_roman(*(4,)) == 'IV'
    assert parsekit.int_to_roman(*(1994,)) == 'MCMXCIV'
    with pytest.raises(ValueError):
        parsekit.int_to_roman(*(0,))

def test_parse_bool():
    assert parsekit.parse_bool(*('Yes',)) == True
    assert parsekit.parse_bool(*(' off ',)) == False
    with pytest.raises(ValueError):
        parsekit.parse_bool(*('maybe',))

def test_parse_version():
    assert parsekit.parse_version(*('1.2.3',)) == (1, 2, 3)
    assert parsekit.parse_version(*('10.0',)) == (10, 0)

def test_parse_range():
    assert parsekit.parse_range(*('1-5',)) == [1, 2, 3, 4, 5]
    assert parsekit.parse_range(*('3-3',)) == [3]
    with pytest.raises(ValueError):
        parsekit.parse_range(*('5-1',))

def test_jaccard():
    assert setkit.jaccard(*([1, 2, 3], [2, 3, 4])) == 0.5
    assert setkit.jaccard(*([], [])) == 1.0

def test_symmetric_diff():
    assert setkit.symmetric_diff(*([1, 2, 3], [2, 3, 4])) == [1, 4]

def test_is_subset():
    assert setkit.is_subset(*([1, 2], [1, 2, 3])) == True
    assert setkit.is_subset(*([1, 4], [1, 2, 3])) == False

def test_union_all():
    assert setkit.union_all(*([[1, 2], [2, 3], [3, 4]],)) == [1, 2, 3, 4]

def test_intersection_all():
    assert setkit.intersection_all(*([[1, 2, 3], [2, 3, 4], [3, 4, 5]],)) == [3]
    assert setkit.intersection_all(*([],)) == []

def test_count_common():
    assert setkit.count_common(*([1, 2, 3], [2, 3, 4])) == 2

def test_unique_to_first():
    assert setkit.unique_to_first(*([1, 2, 3], [2, 3])) == [1]

def test_is_disjoint():
    assert setkit.is_disjoint(*([1, 2], [3, 4])) == True
    assert setkit.is_disjoint(*([1], [1])) == False

def test_powerset_size():
    assert setkit.powerset_size(*([1, 2, 3],)) == 8
    assert setkit.powerset_size(*([1, 1],)) == 2

def test_mode():
    assert setkit.mode(*([1, 2, 2, 3, 3],)) == 2
    assert setkit.mode(*([5],)) == 5
    with pytest.raises(ValueError):
        setkit.mode(*([],))

def test_caesar():
    assert codekit.caesar(*('abc', 1)) == 'bcd'
    assert codekit.caesar(*('XYZ', 3)) == 'ABC'

def test_run_length_encode():
    assert codekit.run_length_encode(*('aaabbc',)) == 'a3b2c1'
    assert codekit.run_length_encode(*('',)) == ''

def test_run_length_decode():
    assert codekit.run_length_decode(*('a3b2c1',)) == 'aaabbc'

def test_to_binary():
    assert codekit.to_binary(*(10,)) == '1010'
    assert codekit.to_binary(*(0,)) == '0'
    with pytest.raises(ValueError):
        codekit.to_binary(*(-1,))

def test_from_binary():
    assert codekit.from_binary(*('1010',)) == 10
    assert codekit.from_binary(*('0',)) == 0

def test_xor_encode():
    assert codekit.xor_encode(*('AB', 1)) == [64, 67]
    assert codekit.xor_encode(*('', 5)) == []

def test_checksum():
    assert codekit.checksum(*('ABC',)) == 198
    assert codekit.checksum(*('',)) == 0

def test_hex_encode():
    assert codekit.hex_encode(*('AB',)) == '4142'

def test_hex_decode():
    assert codekit.hex_decode(*('4142',)) == 'AB'

def test_atbash():
    assert codekit.atbash(*('abc',)) == 'zyx'
    assert codekit.atbash(*('XYZ',)) == 'CBA'
