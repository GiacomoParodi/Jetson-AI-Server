"""Esempio di plugin. Copialo in un file senza '_' iniziale (es. plugins/conta_parole.py)
e riavvia il server: il nuovo compito compare nell'interfaccia.

I file che iniziano con '_' vengono ignorati, per questo questo esempio non è attivo.
"""
from app.tasks import JobContext, Param, Task, TaskError


class ContaParole(Task):
    id = "conta_parole"
    title = "Conta parole"
    description = "Conta le parole di un file di testo e trova le più frequenti."
    accept = [".txt", ".md"]
    params = [
        Param("top", "Quante parole mostrare", "number", default=10, min=1, max=100, step=1),
    ]

    def run(self, ctx: JobContext) -> dict:
        text = ctx.input_path.read_text(encoding="utf-8", errors="replace")
        words = [w.lower() for w in text.split() if w.isalpha()]
        if not words:
            raise TaskError("Il file non contiene parole")

        counts: dict[str, int] = {}
        for i, w in enumerate(words):
            ctx.check_cancelled()  # permette all'utente di annullare
            counts[w] = counts.get(w, 0) + 1
            ctx.progress(i / len(words), "Conteggio…")

        top = sorted(counts.items(), key=lambda kv: -kv[1])[: int(ctx.params["top"])]
        (ctx.output_dir / "conteggio.csv").write_text(
            "parola,volte\n" + "\n".join(f"{w},{n}" for w, n in top), encoding="utf-8"
        )
        return {
            "summary": f"Il file contiene **{len(words)}** parole, di cui {len(counts)} diverse.",
            "table": {"columns": ["Parola", "Volte"], "rows": [[w, n] for w, n in top]},
            "outputs": [{"file": "conteggio.csv", "kind": "file", "label": "Conteggio (CSV)"}],
        }
