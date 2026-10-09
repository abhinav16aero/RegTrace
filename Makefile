.PHONY: setup build demo stop reset-demo test test-unit browser-check
NODE_PATH := $(CURDIR)/.tools/node_modules/.bin

setup:
	python3 -m venv .venv
	.venv/bin/python -I -m pip install -r requirements.lock
	npm ci --prefix .tools
	PATH="$(NODE_PATH):$$PATH" npm ci --prefix web

build:
	PATH="$(NODE_PATH):$$PATH" npm --prefix web run build

demo: setup build
	.venv/bin/python -I scripts/dev.py start

stop:
	.venv/bin/python -I scripts/dev.py stop

reset-demo:
	.venv/bin/python -I scripts/dev.py reset --yes

test:
	.venv/bin/python -I -m pytest -q --junitxml=artifacts/m1/tests.xml

test-unit:
	.venv/bin/python -I -m pytest -q tests/test_domain_contracts.py

browser-check:
	PATH="$(NODE_PATH):$$PATH" web/node_modules/.bin/playwright install chromium
	.tools/node_modules/.bin/node web/smoke.mjs
