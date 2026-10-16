import codecs
import enum
import json
import random
import sys
import threading
import unittest

import _decky_native_json as native


class IntLike(int):
    def __repr__(self):
        return 'wrong'

    def __str__(self):
        return 'wrong'


class FloatLike(float):
    def __repr__(self):
        return 'wrong'


class StringLike(str):
    def encode(self, *args, **kwargs):
        raise RuntimeError('subclass override must not be used')


class ListLike(list):
    def __iter__(self):
        return iter(['wrong'])


class DictLike(dict):
    def items(self):
        return [('wrong', 'wrong')]


class Number(enum.IntEnum):
    VALUE = 3


def outcome(encoder, value, ascii=True):
    try:
        return ('value', encoder(value, ensure_ascii=ascii))
    except Exception as error:
        return ('error', type(error), str(error))


class NativeJsonCompatibility(unittest.TestCase):
    def test_rejects_unsupported_call_forms(self):
        with self.assertRaises(TypeError):
            native.dumps(None, False)
        with self.assertRaises(TypeError):
            native.dumps(None, ensure_ascii=1)

    def test_deterministic_differential_scalars_containers_and_subclasses(self):
        random.seed(71)
        values = [
            None, True, False, 0, -1, 2**63 - 1, 2**63, -(2**63), -(2**63) - 1,
            2**1024, 0.0, -0.0, 1e-7, 1e20, 1.2345678901234567,
            float('nan'), float('inf'), float('-inf'), '', '\x7f',
            '日本語', 'é\u2028😀', '\ud800', '\udc00', '\ud800\udc00',
            ''.join(chr(i) for i in range(128)), (1, 'x', False),
            {None: 'none', False: 'false', 3: 'int', 1.25: 'float', 'x': 'string'},
            {'nested': [None, (1, 2), {'x': '日本語'}]},
            IntLike(5), FloatLike(1.5), StringLike('\ud800'), ListLike([1, 2]),
            DictLike({'real': 2}), Number.VALUE,
        ]
        for _ in range(200):
            text = ''.join(chr(random.randrange(0x110000)) for _ in range(24))
            values.append({
                'text': text,
                'ints': [random.randrange(-(2**80), 2**80) for _ in range(8)],
                'floats': [random.uniform(-1e30, 1e30) for _ in range(8)],
            })
        for index, value in enumerate(values):
            for ascii_mode in (False, True):
                with self.subTest(index=index, ensure_ascii=ascii_mode):
                    expected = json.dumps(value, ensure_ascii=ascii_mode)
                    actual = native.dumps(value, ensure_ascii=ascii_mode)
                    self.assertIs(type(actual), str)
                    self.assertEqual(actual, expected)

    def test_unsupported_objects_and_dictionary_keys_preserve_errors(self):
        long_name = type('漢' * 40, (), {})
        values = [
            object(), b'bytes', {('tuple',): 1}, {object(): 2},
            {long_name(): 1},
        ]
        for value in values:
            self.assertEqual(outcome(native.dumps, value), outcome(json.dumps, value))

    def test_unsupported_object_class_lookup_and_format_callbacks(self):
        class ClassLookupRaises:
            @property
            def __class__(self):
                raise RuntimeError('class lookup failed')

        class FormatName:
            def __format__(self, spec):
                return 'custom\ud800'

        class FormatRaises:
            def __format__(self, spec):
                raise RuntimeError('name formatting failed')

        for name in (FormatName(), FormatRaises(), 'plain\ud800'):
            fake_class = type('ClassDescription', (), {})()
            fake_class.__name__ = name

            class CustomClass:
                @property
                def __class__(self):
                    return fake_class

            self.assertEqual(
                outcome(native.dumps, CustomClass()),
                outcome(json.dumps, CustomClass()),
            )
        self.assertEqual(
            outcome(native.dumps, ClassLookupRaises()),
            outcome(json.dumps, ClassLookupRaises()),
        )

    def test_surrogates_never_call_registered_strict_error_handlers(self):
        original = codecs.lookup_error('strict')
        calls = []

        def replacement(error):
            calls.append('replacement')
            return ('changed', error.end)

        def invalid_bytes(error):
            calls.append('invalid-bytes')
            return (b'\xff', error.end)

        def raises(error):
            calls.append('raises')
            raise RuntimeError('strict handler must not run')

        try:
            for handler in (replacement, invalid_bytes, raises):
                codecs.register_error('strict', handler)
                for ascii_mode in (False, True):
                    for text in (chr(0xd800), chr(0xdc00) + '日本語'):
                        self.assertEqual(
                            native.dumps(text, ensure_ascii=ascii_mode),
                            json.dumps(text, ensure_ascii=ascii_mode),
                        )
            self.assertEqual(calls, [])
        finally:
            codecs.register_error('strict', original)

    def test_snapshot_finalizers_preserve_enclosing_list_output(self):
        def encode(encoder):
            root = []
            mapping = {}

            class Key(str):
                def __del__(self):
                    root.append('key:' + self)

            class Value(list):
                def __iter__(self):
                    mapping.clear()
                    return iter([1])

                def __del__(self):
                    root.append('value')

            mapping[Key('a')] = Value([0])
            mapping[Key('b')] = Value([0])
            root.append(mapping)
            return encoder(root)

        self.assertEqual(encode(native.dumps), encode(json.dumps))

    def test_cycles_shared_references_and_recursion_limit(self):
        cycle = []
        cycle.append(cycle)
        self.assertEqual(outcome(native.dumps, cycle), outcome(json.dumps, cycle))
        shared = [1, 2]
        self.assertEqual(native.dumps([shared, shared]), json.dumps([shared, shared]))
        original_limit = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(100)
            nested = None
            for _ in range(140):
                nested = [nested]
            self.assertEqual(outcome(native.dumps, nested), outcome(json.dumps, nested))
        finally:
            sys.setrecursionlimit(original_limit)

    @staticmethod
    def _observe(encoder, factory, ascii_mode):
        callbacks = []
        value = factory(encoder, callbacks)
        try:
            return {'result': encoder(value, ensure_ascii=ascii_mode), 'callbacks': callbacks}
        except Exception as error:
            return {
                'error_type': type(error).__name__, 'error': str(error),
                'callbacks': callbacks,
            }

    @staticmethod
    def _empty_reentry(encoder, callbacks):
        class ReentrantList(list):
            def __iter__(self):
                self.calls = getattr(self, 'calls', 0) + 1
                callbacks.append(self.calls)
                return iter([self] if self.calls == 1 else [])
        return ReentrantList([0])

    @staticmethod
    def _malformed_mapping(encoder, callbacks):
        class Mapping(dict):
            def items(self):
                return [('first', object()), None]
        return Mapping(seed=1)

    @staticmethod
    def _malformed_after_callback(encoder, callbacks):
        class CallbackList(list):
            def __iter__(self):
                callbacks.append('first-value')
                return iter([1])
        class Mapping(dict):
            def items(self):
                return [('first', CallbackList([1])), None]
        return Mapping(seed=1)

    @staticmethod
    def _mutate_dict_ancestor(encoder, callbacks):
        root = {}
        class CallbackList(list):
            def __iter__(self):
                root['later'] = 3
                callbacks.append('mutated-dict')
                return iter([1])
        root['first'] = CallbackList([1])
        return root

    @staticmethod
    def _mutate_list_ancestor(encoder, callbacks):
        root = []
        class Mapping(dict):
            def items(self):
                root.append(2)
                callbacks.append('mutated-list')
                return [('first', 1)]
        root.append(Mapping(seed=1))
        return root

    @staticmethod
    def _mutate_mapping_items(encoder, callbacks):
        pairs = []
        class CallbackList(list):
            def __iter__(self):
                pairs.append(('later', 2))
                callbacks.append('mutated-items')
                return iter([1])
        class Mapping(dict):
            def items(self):
                return pairs
        pairs.append(('first', CallbackList([1])))
        return Mapping(seed=1)

    @staticmethod
    def _nested_encoding(encoder, callbacks):
        class CallbackList(list):
            def __iter__(self):
                callbacks.append(encoder({'nested': [1, '日本語']}, ensure_ascii=False))
                return iter([2])
        return {'first': CallbackList([0])}

    @staticmethod
    def _threaded_mutation(encoder, callbacks):
        root = {}
        class CallbackList(list):
            def __iter__(self):
                def mutate():
                    root['from-thread'] = 4
                thread = threading.Thread(target=mutate)
                thread.start()
                thread.join(timeout=10)
                if thread.is_alive():
                    raise AssertionError('mutation thread failed to finish')
                callbacks.append('thread-finished')
                return iter([1])
        root['first'] = CallbackList([0])
        return root

    @staticmethod
    def _changing_codec_registry(encoder, callbacks):
        return '\ud800日本語\udc00'

    def test_callback_order_reentry_mutation_and_surrogate_codec_independence(self):
        factories = (
            self._empty_reentry, self._malformed_mapping,
            self._malformed_after_callback, self._mutate_dict_ancestor,
            self._mutate_list_ancestor, self._mutate_mapping_items,
            self._nested_encoding, self._threaded_mutation,
            self._changing_codec_registry,
        )
        for factory in factories:
            original_handler = codecs.lookup_error('surrogatepass')
            if factory.__name__ == '_changing_codec_registry':
                codecs.register_error('surrogatepass', lambda error: ('changed', error.end))
            try:
                for ascii_mode in (False, True):
                    with self.subTest(case=factory.__name__, ensure_ascii=ascii_mode):
                        expected = self._observe(json.dumps, factory, ascii_mode)
                        actual = self._observe(native.dumps, factory, ascii_mode)
                        self.assertEqual(actual, expected)
            finally:
                codecs.register_error('surrogatepass', original_handler)

    def test_near_default_recursion_results_and_errors_match(self):
        original_limit = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(1000)
            for depth in (800, 1200):
                value = None
                for _ in range(depth):
                    value = [value]
                self.assertEqual(outcome(native.dumps, value), outcome(json.dumps, value))
        finally:
            sys.setrecursionlimit(original_limit)


if __name__ == '__main__':
    unittest.main()
