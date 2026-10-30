"""Дочерний процесс хаос-проверки: сообщает точную фазу и ждёт SIGKILL."""
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"src"))

from iam.domain import Op, new_id
from iam.session import SessionRunner
from iam.state import StateController
from iam.storage import Store


def main(path,phase):
    with Store.open(path) as store:
        ctl=StateController(store)
        run=SessionRunner(ctl)
        if ctl.session_state=="paused":run.command("stop","подготовка хаос-прогона")
        run.command("start","хаос-проверка",max_calls=1)
        if phase=="reserved":
            run.charge()
        elif phase=="mid":
            # Внешняя транзакция теста удерживает запись перехода незавершённой.
            with store.transaction():
                ctl.intervene("technical_fix","проверка незавершённой записи",(Op("beliefs","chaos","set",phase,True),))
                print("READY",flush=True)
                sys.stdin.read()
        print("READY",flush=True)
        sys.stdin.read()


if __name__=="__main__":main(*sys.argv[1:3])
