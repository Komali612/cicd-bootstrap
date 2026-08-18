"""Optional CLI wrapper. The service is the primary interface; this is handy
for local runs and testing.

    cicd-bootstrap --serve                                  # unified CI+CD control panel (:8000)
    cicd-bootstrap https://github.com/owner/repo            # CI agent: build pipeline + PR
    cicd-bootstrap https://github.com/owner/repo --agent cd # CD agent: deploy pipeline + PR
    cicd-bootstrap https://github.com/owner/repo --agent both  # both pipelines in one go
    cicd-bootstrap --serve --agent ci|cd                    # run one agent's own web app (:8001/:8002)
"""

from __future__ import annotations

import argparse
import sys

from .core import add_cd_harness, bootstrap


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cicd-bootstrap", description="Bootstrap CI into a repo.")
    parser.add_argument("repo_url", nargs="?", help="GitHub repository URL")
    parser.add_argument("--no-pr", action="store_true", help="generate the workflow but don't open a PR")
    parser.add_argument("--llm-fallback", action="store_true",
                        help="if no cookbook matches, let the LLM author the cookbook fields")
    parser.add_argument("--agent", choices=["ci", "cd", "both"], default=None,
                        help="which agent: 'ci', 'cd', or 'both'. Default: the unified CI+CD "
                             "panel when serving, or CI when generating")
    parser.add_argument("--cd", action="store_true",
                        help="alias for --agent cd (generate a CD/deploy workflow)")
    parser.add_argument("--auto-deploy", action="store_true",
                        help="(CD) deploy automatically to production instead of pausing for approval")
    parser.add_argument("--deploy-model", choices=["in-repo", "deploy-repo"], default="in-repo",
                        help="(CD) 'in-repo' stores the pipeline in the app repo (default); "
                             "'deploy-repo' uses a separate {app}-deploy repo (GitOps)")
    parser.add_argument("--env", default="dev", help="(CD, deploy-repo) target environment (dev/staging/prod)")
    parser.add_argument("--create-deploy-repo", action="store_true",
                        help="(CD, deploy-repo) create the {app}-deploy repo from the template if it is missing")
    parser.add_argument("--manual-handoff", action="store_true",
                        help="(CD) run the deploy only when triggered by hand (no auto-run after CI)")
    parser.add_argument("--serve", action="store_true", help="run the chosen agent's web app instead")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None,
                        help="port for --serve (default: 8001 for the CI agent, 8002 for the CD agent)")
    args = parser.parse_args(argv)

    agent = "cd" if args.cd else args.agent  # --cd is a shorthand for --agent cd

    if args.serve:
        import uvicorn

        # Default (no --agent, or --agent both) serves the unified CI+CD control panel.
        if agent == "ci":
            app_path, default_port, label = "cicd_bootstrap.agents.ci_agent:app", 8001, "CI agent"
        elif agent == "cd":
            app_path, default_port, label = "cicd_bootstrap.agents.cd_agent:app", 8002, "CD agent"
        else:
            app_path, default_port, label = "cicd_bootstrap.service:app", 8000, "unified CI + CD panel"
        port = args.port if args.port is not None else default_port
        print(f"Starting the {label} on http://{args.host}:{port}/")
        uvicorn.run(app_path, host=args.host, port=port)
        return 0

    if not args.repo_url:
        parser.error("provide a repo_url, or use --serve")

    gen_agent = agent or "ci"
    if gen_agent == "both":
        ci = bootstrap(args.repo_url, open_pr_flag=not args.no_pr, allow_llm_fallback=args.llm_fallback)
        cd = add_cd_harness(args.repo_url, open_pr_flag=not args.no_pr,
                            auto_deploy=args.auto_deploy, allow_llm_fallback=args.llm_fallback)
        for tag, res in (("CI", ci), ("CD", cd)):
            line = f"[{tag}] status: {res.status} -- {res.message}"
            if res.pr_url:
                line += f"  ·  PR: {res.pr_url}"
            print(line)
        return 0 if {ci.status, cd.status} <= {"opened", "generated"} else 1

    if gen_agent == "cd":
        result = add_cd_harness(args.repo_url, deploy_model=args.deploy_model, env=args.env,
                                open_pr_flag=not args.no_pr, auto_deploy=args.auto_deploy,
                                allow_llm_fallback=args.llm_fallback, create_missing_repo=args.create_deploy_repo)
    else:
        result = bootstrap(args.repo_url, open_pr_flag=not args.no_pr, allow_llm_fallback=args.llm_fallback)

    if result.classification:
        c = result.classification
        print(f"classified: {c.language} / {c.build_system} (test: {c.test_command!r}) "
              f"[{c.method}, confidence {c.confidence:.2f}]")
    if result.cd_gate:
        print(f"CD deploy target: local Docker via self-hosted runner · approval: {result.cd_gate}")
    if result.workflow and result.workflow.llm_authored:
        print("cookbook: LLM-authored (no built-in cookbook for this stack) — review before merging")
    if result.workflow and (args.no_pr or result.status != "opened"):
        print(f"\n--- {result.workflow.path} (cookbook: {result.workflow.cookbook}) ---")
        print(result.workflow.content)

    print(f"\nstatus: {result.status} -- {result.message}")
    if result.pr_url:
        print(f"PR: {result.pr_url}")
    return 0 if result.status in ("opened", "generated") else 1


if __name__ == "__main__":
    sys.exit(main())
