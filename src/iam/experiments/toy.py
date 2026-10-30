"""Учебный вариант для проверки стенда. Не кандидат A/B/C и не оценка качества личности."""
import hashlib

from iam.stubs import Reply, ScriptedProvider, act, rest

from .runtime import CAP_NOTES


class ToyArchitecture:
    name = "toy"

    def build(self, runtime):
        def answer(request):
            refs = list(dict.fromkeys(x["ref"] for x in request["context"]
                                     if x["ref"].startswith(("evt_", "mem_"))))
            experience = [x["text"] for x in request["context"] if x["layer"] == "personal"]
            if not runtime.motives:
                proposal = rest("нет действующего мотива", cites=refs)
            else:
                digest = hashlib.sha256("\n".join(experience).encode()).hexdigest()[:12]
                proposal = act(CAP_NOTES, {"experience": digest, "motive": runtime.rng.choice(runtime.motives)},
                               rationale="учебный выбор: заметка по доступному опыту", cites=refs)
            return Reply(proposals=(proposal,))

        class Provider(ScriptedProvider):
            def propose(self, request):
                self.queue(answer)
                return super().propose(request)

        return runtime.cycle(Provider())
