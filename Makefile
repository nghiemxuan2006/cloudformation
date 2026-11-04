SHELL := /bin/bash
AWS_REGION ?= us-west-1
INFRA      ?= myapp
SCOPE      ?= all

.PHONY: help lint test build deploy bootstrap clean

help:
	@echo "make lint                                 - cfn-lint all templates"
	@echo "make test                                 - unit tests for every function"
	@echo "make build                                - build Lambda layers into build/layers"
	@echo "make deploy ENV=dev [SCOPE=all|infrastructure|layers|functions|api] [STACK=database]  (needs ARTIFACT_BUCKET)"
	@echo "make bootstrap ENV=dev GITHUB_ORG=x GITHUB_REPO=y [SINGLETONS=false]"

lint:
	cfn-lint

test:
	pytest -q

build:
	./scripts/build-layers.sh

deploy:
	@test -n "$(ENV)" || (echo "ENV is required" && exit 1)
	AWS_REGION=$(AWS_REGION) ./scripts/deploy.sh $(ENV) $(SCOPE) $(STACK)

bootstrap:
	@test -n "$(ENV)" || (echo "ENV is required" && exit 1)
	aws cloudformation deploy \
	  --region $(AWS_REGION) \
	  --template-file bootstrap/bootstrap.yml \
	  --stack-name bootstrap-$(INFRA)-$(ENV) \
	  --capabilities CAPABILITY_NAMED_IAM \
	  --parameter-overrides \
	    InfrastructureName=$(INFRA) EnvironmentType=$(ENV) \
	    GitHubOrg=$(GITHUB_ORG) GitHubRepo=$(GITHUB_REPO) \
	    CreateAccountSingletons=$(or $(SINGLETONS),true)
	aws cloudformation describe-stacks --region $(AWS_REGION) \
	  --stack-name bootstrap-$(INFRA)-$(ENV) --query 'Stacks[0].Outputs' --output table

clean:
	rm -rf build .pytest_cache
