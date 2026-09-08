.PHONY: up archive stop start down logs sweep investigate eval test lint synth deploy update stop-aws start-aws destroy

up:            ## Local stack (core + observability)
	docker compose up --build -d
	@echo "Argus UI http://localhost:8088  API http://localhost:8000/docs  Grafana http://localhost:3000 (admin/admin)"

archive:       ## Export expired positions partitions to Parquet and drop them (HOT_DAYS=0 DRY_RUN=true to preview)
	docker compose --profile maintenance run --rm archiver

stop:          ## Pause the local stack, keep every volume
	docker compose stop

start:         ## Resume a paused local stack
	docker compose start

down:          ## Delete the local stack INCLUDING volumes
	docker compose down -v

logs:
	docker compose logs -f agent-orchestrator agent-watch agent-investigator agent-tasking

sweep:         ## Ask the watch agent to sweep the last 12 hours
	curl -s -X POST "http://localhost:8000/sweep?hours=12"

investigate:   ## Investigate MERIDIAN STAR (dark gap in the cable corridor)
	curl -s -X POST http://localhost:8000/investigations/511666006 -H 'Content-Type: application/json' -d '{"trigger":"manual"}'

eval:
	python evals/run_eval.py --api http://localhost:8000

eval-aws:      ## Node evals against the AWS stack as the argus-operator role
	scripts/eval_aws.sh

rekey:         ## Re-encrypt personal data under a new data key (local stack; prints the key for .env)
	docker compose exec api python rekey.py

rekey-aws:     ## Same on AWS: one-off task on the API image, stores the key in the secret
	scripts/rekey_aws.sh

test:
	pytest -q tests

lint:
	ruff check .

diagrams:      ## Render docs/diagrams/*.png from src/*.html (needs Chrome) and check the counts they state
	sh docs/diagrams/src/render.sh
	python3 docs/diagrams/src/check_counts.py

tools-inventory:  ## Regenerate mcp-servers/tools.json from the servers (registry records, Cedar policies)
	docker compose build -q mcp-ais && docker run --rm -v $(PWD)/mcp-servers/inventory.py:/app/inventory.py -v $(PWD)/mcp-servers:/out $$(docker compose images -q mcp-ais | head -1) python /app/inventory.py /out/tools.json

dashboards:    ## Regenerate the Grafana board (local and AWS variants) from observability/grafana/build_dashboards.py
	python3 observability/grafana/build_dashboards.py

synth:
	cd infra/cdk && cdk synth --all -q

deploy:        ## AWS: create or update the whole stack (CDK bootstrap + 4 stacks)
	./scripts/deploy.sh

update: deploy ## AWS: same as deploy; CDK diffs and applies in place

stop-aws:      ## AWS: scale every service to zero and let Aurora auto-pause (stack stays)
	PAUSED=true ./scripts/deploy.sh

start-aws:     ## AWS: resume a stopped stack
	PAUSED=false ./scripts/deploy.sh

destroy:       ## AWS: delete all four stacks and garbage-collect bootstrap assets
	./scripts/destroy.sh
