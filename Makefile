.PHONY: install test lint baseline run compose-baseline compose-run
install:
	./install.sh
test:
	pytest -q
lint:
	ruff check .
baseline:
	fstec-monitor baseline
run:
	fstec-monitor run
compose-baseline:
	docker compose run --rm monitor fstec-monitor baseline
compose-run:
	docker compose run --rm monitor fstec-monitor run
