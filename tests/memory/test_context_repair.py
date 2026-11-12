from social.test_context_repair import ContextCase


class MemoryWindowContextTest(ContextCase):
    def test_old_personal_window_identifies_own_unknown_words(self):
        source, answer = self.exchange('Что скажешь о поездке?', 'Пока не помню подробностей.', outcome='unknown')
        hits = self.distract()
        request, _, _ = self.request('Что было сказано о поездке?', [answer, source, *hits[:3]])
        packet = next(packet for packet in self.packets(request, answer.memory_id) if 'text_window' in packet)
        self.assertEqual(packet.get('speaker'), 'self')
        self.assertEqual(packet['outcome'], 'unknown')
        self.assertEqual(packet['text'], answer.text)
