"""CLI : publier, admettre, faire tourner, lire le journal, répondre."""

import argparse
import json
import sys
from pathlib import Path

from . import channel, db, graph, kernel, scheduler, web


def main() -> None:
    p = argparse.ArgumentParser(prog="graphatom")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init-db", help="créer les tables")
    sp.add_argument("--drop", action="store_true")

    sp = sub.add_parser("publish", help="valider et publier un bundle JSON")
    sp.add_argument("bundle", type=Path)

    sp = sub.add_parser("admit", help="admettre une occurrence")
    sp.add_argument("revision")
    sp.add_argument("subject_key")

    sub.add_parser("run", help="ordonnanceur, boucle infinie")
    sub.add_parser("tick", help="un seul tick d'ordonnanceur")
    sub.add_parser("items", help="lister les items")
    sub.add_parser("questions", help="questions ouvertes")

    sp = sub.add_parser("journal", help="trajectoire d'un item")
    sp.add_argument("item_id", type=int)

    sp = sub.add_parser("answer", help="répondre à une question")
    sp.add_argument("question_id", type=int)
    sp.add_argument("option")
    sp.add_argument("--by", default="jack")

    sp = sub.add_parser("serve", help="canal humain : web local sur les questions")
    sp.add_argument("--port", type=int, default=8848)
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--by", default="jack")
    sp.add_argument("--notify-cmd", default=None,
                    help="commande shell lancée à chaque question ouverte (JSON sur stdin)")

    sp = sub.add_parser("github-sync", help="canal github : issues labellisées, /answer, rapports")
    sp.add_argument("--repo", required=True, help="owner/repo")
    sp.add_argument("--graph", required=True, help="bundle JSON publié au démarrage")
    sp.add_argument("--poll", type=float, default=15.0)

    args = p.parse_args()

    if args.cmd == "init-db":
        db.init_db(drop=args.drop)
        print("base initialisée")
        return
    if args.cmd == "run":
        scheduler.run_forever()
        return
    if args.cmd == "serve":
        web.serve(port=args.port, by=args.by, notify_cmd=args.notify_cmd, host=args.host)
        return
    if args.cmd == "github-sync":
        from . import github_sync
        github_sync.sync_forever(args.repo, args.graph, poll_s=args.poll)
        return

    with db.connect() as conn:
        if args.cmd == "publish":
            rev = graph.publish(conn, json.loads(args.bundle.read_text()))
            print(rev)
        elif args.cmd == "admit":
            print(kernel.admit(conn, args.revision, args.subject_key))
        elif args.cmd == "tick":
            print(scheduler.tick(conn))
        elif args.cmd == "items":
            for r in conn.execute(
                "SELECT w.id, s.subject_key, w.generation, w.state, w.version, "
                "w.escalations, w.terminal_at FROM work_item w "
                "JOIN subject s ON s.id = w.subject_id ORDER BY w.id"
            ):
                fin = r["terminal_at"].strftime("%H:%M:%S") if r["terminal_at"] else "actif"
                print(f"#{r['id']} {r['subject_key']} g{r['generation']} "
                      f"état={r['state']} v{r['version']} esc={r['escalations']} {fin}")
        elif args.cmd == "questions":
            for r in conn.execute(
                "SELECT * FROM question WHERE state = 'open' ORDER BY id"
            ):
                print(f"[{r['id']}] item {r['item_id']} → {r['owner']} "
                      f"(avant {r['deadline']:%H:%M})\n    {r['text']}\n"
                      f"    options : {', '.join(r['options'])}")
        elif args.cmd == "journal":
            for r in conn.execute(
                "SELECT * FROM event WHERE item_id = %s ORDER BY item_version",
                (args.item_id,),
            ):
                arrow = f"{r['from_state']} → " if r["from_state"] else ""
                out = f" [{r['outcome']}]" if r["outcome"] else ""
                print(f"v{r['item_version']:>2} {r['at']:%H:%M:%S} {r['kind']:<9} "
                      f"{arrow}{r['to_state']}{out}")
        elif args.cmd == "answer":
            err = channel.record_answer(conn, args.question_id, args.option, args.by)
            if err:
                sys.exit(err)
            print("réponse enregistrée — le waiter routera au prochain tick")


if __name__ == "__main__":
    main()
