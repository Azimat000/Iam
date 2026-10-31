"""Изолированный стенд S19: только искусственные записи, без вызовов модели и Telegram."""
from dataclasses import replace
from pathlib import Path
import uvicorn
from iam.panel.app import create_app
from iam.panel.runtime import Runtime

class Preview(Runtime):
    def _setup(self):
        super()._setup()
        if not self.ctl.readable_memories():
            self.ctl.start_session()
            m = self.cycle.memory
            r = m.prepare(kind='episodic', origin='own_hypothesis', text='Проверочная архивная деталь: героиню зовут Лидия. <script>не выполнять</script>',
                          sources=(self.store.get_event(1).event_id,))
            r = replace(r, memory_id='mem_S19Restore001')
            assert m.write(r).applied
            assert m.fade(r.memory_id).applied
            self.ctl.pause_session('operator')

runtime = Preview(Path('/Users/azamatsibgatullin/.iam-evidence/s19-memory-20261101/preview-final.db'), demo=True)
uvicorn.run(create_app(runtime, port=18769), host='127.0.0.1', port=18769, access_log=False)
