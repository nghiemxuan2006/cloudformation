SHELL := /bin/bash
AWS_REGION ?= ap-southeast-1
PROJECT    ?= myapp

.PHONY: help lint test build plan deploy bootstrap clean

help:
	@echo "make lint                      - cfn-lint all templates"
	@echo "make test                      - run unit tests"
	@echo "make build                     - build Lambda package into build/lambda"
	@echo "make plan   ENV=dev            - create & show change set only"
	@echo "make deploy ENV=dev            - package + deploy (needs ARTIFACT_BUCKET)"
	@echo "make bootstrap ENV=dev GITHUB_ORG=x GITHUB_REPO=y [CREATE_OIDC=false]"

lint:
	cfn-lint

test:
	pytest -q

build:
	./scripts/build-lambda.sh

plan:
	@test -n "$(ENV)" || (echo "ENV is required" && exit 1)
	AWS_REGION=$(AWS_REGION) ./scripts/deploy.sh $(ENV) --plan

deploy:
	@test -n "$(ENV)" || (echo "ENV is required" && exit 1)
	AWS_REGION=$(AWS_REGION) ./scripts/deploy.sh $(ENV)

bootstrap:
	@test -n "$(ENV)" || (echo "ENV is required" && exit 1)
	aws cloudformation deploy \
	  --region $(AWS_REGION) \
	  --template-file bootstrap/bootstrap.yaml \
	  --stack-name bootstrap-$(PROJECT)-$(ENV) \
	  --capabilities CAPABILITY_NAMED_IAM \
	  --parameter-overrides \
	    ProjectName=$(PROJECT) Environment=$(ENV) \
	    GitHubOrg=$(GITHUB_ORG) GitHubRepo=$(GITHUB_REPO) \
	    CreateOidcProvider=$(or $(CREATE_OIDC),true)
	aws cloudformation describe-stacks --region $(AWS_REGION) \
	  --stack-name bootstrap-$(PROJECT)-$(ENV) --query 'Stacks[0].Outputs' --output table

clean:
	rm -rf build .pytest_cache
