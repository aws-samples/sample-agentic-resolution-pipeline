# Agentic Resolution Pipeline — Makefile
#
# Usage:
#   make deploy-all VPC_ID=vpc-xxx         Deploy everything (pipeline + sample app)
#   make deploy-pipeline VPC_ID=vpc-xxx    Deploy pipeline stacks only
#   make deploy-iot VPC_ID=vpc-xxx         Deploy IoT sample app only
#   make test-e2e                          Run a test ticket through the pipeline
#   make build-frontend                    Build the IoT dashboard
#   make clean                             Remove build artifacts

# ── Configuration ─────────────────────────────────────────────────────────────
REGION ?= us-east-1

# Activate venv if present and not already active
ACTIVATE_VENV = if [ -z "$$VIRTUAL_ENV" ] && [ -d .venv ]; then . .venv/bin/activate; fi

# Validate cdk.context.json exists
check-context:
	@test -f infrastructure/cdk.context.json || \
		(echo "❌ infrastructure/cdk.context.json not found. Copy from cdk.context.json.example and fill in your values." && exit 1)

# ── Deploy targets ────────────────────────────────────────────────────────────

.PHONY: deploy-all deploy-pipeline deploy-iot deploy-frontend

deploy-all: deploy-pipeline deploy-iot deploy-frontend
	@echo "✅ All stacks deployed"

deploy-pipeline: check-context
	@echo "🚀 Deploying pipeline stacks..."
	@$(ACTIVATE_VENV) && \
	cd infrastructure && \
	AWS_REGION=$(REGION) CDK_DEFAULT_REGION=$(REGION) \
	cdk deploy --all --require-approval never
	@echo "✅ Pipeline deployed"

deploy-iot: check-context
	@echo "🚀 Deploying IoT Fleet Management..."
	@$(ACTIVATE_VENV) && \
	cd sample-app/iot-fleet-management/infrastructure && \
	AWS_REGION=$(REGION) CDK_DEFAULT_REGION=$(REGION) \
	cdk deploy IoTFleetStack --require-approval never
	@echo "✅ IoT Fleet app deployed"

deploy-frontend: build-frontend
	@echo "🚀 Deploying frontend (included in IoT stack)..."
	@echo "Frontend is deployed as part of deploy-iot (S3 + CloudFront)"

# ── Build targets ─────────────────────────────────────────────────────────────

.PHONY: build-frontend install-deps

build-frontend:
	@echo "📦 Building IoT Fleet dashboard..."
	cd sample-app/iot-fleet-management/frontend && \
	npm install && npm run build
	@echo "✅ Frontend built at sample-app/iot-fleet-management/frontend/dist/"

install-deps:
	@echo "📦 Installing Python dependencies..."
	pip install -r infrastructure/requirements.txt
	cd sample-app/iot-fleet-management/infrastructure && pip install -r requirements.txt
	@echo "✅ Dependencies installed"

# ── Test targets ──────────────────────────────────────────────────────────────

.PHONY: test-e2e test-unit synth

test-e2e:
	@echo "🧪 Creating test ticket in Jira (IOT project)..."
	@echo "This will trigger the full pipeline: Classify → Investigate → Plan → Fix → PR"
	@echo ""
	@echo "Prerequisites:"
	@echo "  - Pipeline stacks deployed"
	@echo "  - Jira webhook configured for IOT project"
	@echo "  - DevOps Agent space configured with skill"
	@echo ""
	@echo "To run manually:"
	@echo "  1. Create a Bug in the IOT Jira project"
	@echo "  2. Watch Step Functions execution in the AWS console"
	@echo "  3. Approve when prompted (transition to 'In Review', comment '/approve-plan')"
	@echo "  4. Check Bitbucket for the PR"

test-unit:
	@echo "🧪 Running unit tests..."
	cd sample-app/iot-fleet-management/services/telemetry-ingest && \
	pip install -r requirements.txt -q && pytest tests/ -v
	cd sample-app/iot-fleet-management/services/firmware-service && \
	pip install -r requirements.txt -q && pytest tests/ -v
	cd sample-app/iot-fleet-management/services/alert-engine && \
	npm install --silent && npm test
	cd sample-app/iot-fleet-management/services/geofence-service && \
	npm install --silent && npm test
	@echo "✅ All unit tests passed"

synth:
	@echo "🔍 Synthesizing CDK stacks..."
	@$(ACTIVATE_VENV) && \
	cd infrastructure && \
	AWS_REGION=$(REGION) CDK_DEFAULT_REGION=$(REGION) \
	cdk synth  --quiet
	@echo "✅ Synth successful"

# ── Destroy targets ───────────────────────────────────────────────────────────

.PHONY: destroy-pipeline destroy-iot destroy-all

destroy-pipeline:
	@echo "🗑️  Destroying pipeline stacks..."
	@$(ACTIVATE_VENV) && \
	cd infrastructure && \
	AWS_REGION=$(REGION) CDK_DEFAULT_REGION=$(REGION) \
	cdk destroy --all  --force
	@echo "✅ Pipeline stacks destroyed"

destroy-iot:
	@echo "🗑️  Destroying IoT Fleet stack..."
	@$(ACTIVATE_VENV) && \
	cd sample-app/iot-fleet-management/infrastructure && \
	AWS_REGION=$(REGION) CDK_DEFAULT_REGION=$(REGION) \
	cdk destroy IoTFleetStack  --force
	@echo "✅ IoT Fleet stack destroyed"

destroy-all: destroy-pipeline destroy-iot
	@echo "✅ All stacks destroyed"

# ── Clean targets ─────────────────────────────────────────────────────────────

.PHONY: clean clean-cdk clean-frontend

clean: clean-cdk clean-frontend
	@echo "🧹 Cleaned"

clean-cdk:
	rm -rf infrastructure/cdk.out infrastructure/cdk.out.*
	rm -rf sample-app/iot-fleet-management/infrastructure/cdk.out*

clean-frontend:
	rm -rf sample-app/iot-fleet-management/frontend/dist
	rm -rf sample-app/iot-fleet-management/frontend/node_modules

# ── Info targets ──────────────────────────────────────────────────────────────

.PHONY: info

info:
	@echo "═══════════════════════════════════════════════════════"
	@echo " Agentic Resolution Pipeline"
	@echo "═══════════════════════════════════════════════════════"
	@echo ""
	@echo " Pipeline Stacks:"
	@echo "   GuardrailStack, KnowledgeBaseStack, ResolutionPlannerStack,"
	@echo "   ResolutionAgentCoreStack, OrchestratorStack, JiraIntakeStack,"
	@echo "   ResolutionStack"
	@echo ""
	@echo " Sample App:"
	@echo "   IoTFleetStack (4 ECS services + CloudFront dashboard)"
	@echo ""
	@echo " Key Commands:"
	@echo "   make deploy-all VPC_ID=vpc-xxx    Deploy everything"
	@echo "   make deploy-pipeline VPC_ID=vpc-xxx"
	@echo "   make deploy-iot VPC_ID=vpc-xxx"
	@echo "   make build-frontend"
	@echo "   make test-unit"
	@echo "   make synth VPC_ID=vpc-xxx"
	@echo "   make clean"
	@echo ""
	@echo " Docs:"
	@echo "   docs/DEPLOYMENT_GUIDE.md    Full deployment guide"
	@echo "   docs/ADR.md                 Architecture decisions"
	@echo "   docs/PIPELINE_FLOW.md       Pipeline flow diagram"
	@echo "   docs/MEMORY_INTEGRATION.md  AgentCore Memory design"
	@echo ""
