# myapp – Serverless API trên AWS (CloudFormation)

Client → **API Gateway (REST)** → **Lambda** (mỗi endpoint/method một function, chạy trong private subnet) → EFS, Aurora PostgreSQL Serverless v2, ElastiCache (Valkey), DynamoDB, S3, Secrets Manager.

Xử lý bất đồng bộ: `POST /jobs` → **SQS** (`jobs`, kèm DLQ) → Lambda **process-job** (trigger bằng event source mapping) → S3.

VPC **không có NAT Gateway**. Lambda gọi các dịch vụ AWS hoàn toàn qua đường private:

| Dịch vụ | Cách Lambda kết nối |
|---|---|
| S3, DynamoDB | Gateway VPC Endpoint (miễn phí) |
| Secrets Manager, SQS | Interface VPC Endpoint (PrivateLink) |
| Aurora, ElastiCache, EFS | Nằm trong cùng private subnet, kiểm soát bằng Security Group |

> Sơ đồ vẽ "Endpoint" cho ElastiCache, nhưng ElastiCache không có VPC endpoint kiểu PrivateLink cho data plane. Cluster được đặt thẳng trong private subnet, và Lambda kết nối qua Security Group (port 6379, có TLS).

## Cấu trúc thư mục

Theo cùng quy ước với `cfn101-workshop/templates`: các stack độc lập, liên kết với nhau bằng `Export` / `!ImportValue`.

```
.
├── templates/
│   ├── infrastructure/              # Stack nền, mỗi file một stack, deploy theo thứ tự từ trên xuống
│   │   ├── private-vpc.yml          #   VPC, subnet, security group, VPC endpoint, flow logs
│   │   ├── storage.yml              #   S3, DynamoDB, EFS (+ mount target, access point)
│   │   ├── queue.yml                #   SQS jobs + dead-letter queue + alarm khi DLQ có message
│   │   ├── database.yml             #   Aurora PostgreSQL Serverless v2 (secret do RDS quản lý)
│   │   ├── cache.yml                #   ElastiCache Valkey (TLS)
│   │   ├── secrets.yml              #   Secret của ứng dụng
│   │   ├── role.yml                 #   IAM managed policy dùng chung (mỗi policy một năng lực)
│   │   └── api-gateway.yml          #   REST API + request validator
│   ├── lambda-functions/            # Lambda không thuộc API (worker, trigger từ SQS/EventBridge…)
│   │   └── process-job/
│   │       ├── template.yml         #   function + IAM role riêng
│   │       ├── lambda_function.yml  #   version + alias "live" + SQS trigger (event source mapping)
│   │       └── src/lambda_function.py
│   ├── layers/
│   │   └── common/
│   │       ├── layer.yml            #   Lambda layer
│   │       ├── requirements.txt     #   pg8000, redis
│   │       └── python/common/       #   code dùng chung: clients.py, http.py
│   └── api-gateways/
│       ├── stage.yml                # Stage <env>, deploy cuối cùng
│       ├── health/
│       │   ├── main_template.yml    #   resource /health, /health/deps
│       │   ├── GET/                 #   GET /health
│       │   │   ├── template.yml         function + IAM role riêng + API method
│       │   │   ├── lambda_function.yml  version + alias "live" + invoke permission
│       │   │   └── src/lambda_function.py
│       │   └── deps/GET/            #   GET /health/deps
│       └── items/
│           ├── main_template.yml    #   resource /items, /items/{id}
│           ├── POST/                #   POST /items
│           └── {id}/GET/            #   GET /items/{id}
│       └── jobs/
│           ├── main_template.yml    #   resource /jobs
│           └── POST/                #   POST /jobs → gửi message vào SQS
├── parameters/                      # Cấu hình riêng mỗi môi trường: dev.json, test.json, prod.json
├── bootstrap/bootstrap.yml          # Chạy 1 lần/môi trường: artifact bucket, OIDC, IAM cho CI/CD
├── scripts/
│   ├── build-layers.sh              # Build templates/layers/* vào build/layers/*
│   └── deploy.sh                    # Deploy 1 môi trường theo thứ tự các stack
├── tests/                           # Unit test cho từng function
└── .github/workflows/
    ├── ci.yml                       # PR: lint + test + build
    ├── _deploy.yml                  # reusable: env theo nhánh, lint, OIDC, deploy.sh, smoke test
    ├── infra-<stack>.yml            # 1 pipeline / 1 stack cố định trong templates/infrastructure
    ├── layers.yml                   # layers → functions + api
    ├── functions.yml                # lambda-functions/**
    ├── api.yml                      # api-gateways/**
    └── deploy-all.yml               # thủ công: tạo môi trường mới theo đúng thứ tự
```

### Quy ước đặt tên (do `scripts/deploy.sh` tự suy ra từ đường dẫn)

| Thư mục | Resource key (export) | Function / stack | Quyền invoke |
|---|---|---|---|
| `health/GET` | `health` | `myapp-dev-fn-health-get` | `GET/health` |
| `health/deps/GET` | `health-deps` | `myapp-dev-fn-health-deps-get` | `GET/health/deps` |
| `items/POST` | `items` | `myapp-dev-fn-items-post` | `POST/items` |
| `items/{id}/GET` | `items-id` | `myapp-dev-fn-items-id-get` | `GET/items/*` |

- Tên mọi export đều có dạng `<InfrastructureName>-<EnvironmentType>-<Tên>`, ví dụ `myapp-dev-PrivateSubnet1`. Các môi trường dùng chung một account vẫn không đụng nhau.
- Tên mọi stack đều có dạng `<InfrastructureName>-<EnvironmentType>-<component>`, ví dụ `myapp-dev-database`.

### Thêm một endpoint mới, ví dụ `DELETE /items/{id}`

1. Tạo `templates/api-gateways/items/{id}/DELETE/` và copy 3 file từ `{id}/GET/`.
2. Sửa `template.yml`: chỉ gắn những managed policy cần dùng (ở đây là `ItemsTableReadWritePolicy`) và các biến môi trường cần thiết.
3. Viết `src/lambda_function.py`.

Pipeline sẽ tự phát hiện thư mục mới, rồi đặt tên và deploy `myapp-<env>-fn-items-id-delete`. Nếu cần một path mới, thêm resource vào `main_template.yml` của API đó và export với tên `ApiResource-<key>`.

### Thêm một Lambda không thuộc API (worker)

1. Tạo `templates/lambda-functions/<tên>/`, copy 3 file từ `process-job/`.
2. Trong `lambda_function.yml`, đổi trigger (`EventSourceMapping` cho SQS/Kinesis/DynamoDB Streams, `AWS::Events::Rule` + `AWS::Lambda::Permission` cho EventBridge…).
3. Pipeline `functions` sẽ deploy thành stack `myapp-<env>-fn-<tên>` và `…-fn-<tên>-alias`.

### Khác với workshop gốc ở đâu, và vì sao

| Workshop | Ở đây | Lý do |
|---|---|---|
| Export `RestApiId`, `LambdaExecutionRole`… không kèm môi trường | `myapp-<env>-RestApiId` | Tên export là duy nhất trong mỗi region. Nếu dev và test chung account, export không kèm môi trường sẽ xung đột |
| Một function dùng chung cho mọi môi trường, phân biệt bằng alias `dev`/`test` | Mỗi môi trường có function riêng, alias `live` | Mỗi môi trường có VPC, DB và cache riêng. Deploy test cũng không ghi đè code của dev |
| Một `LambdaExecutionRole` dùng chung | Mỗi function có role riêng, ghép từ các policy trong `role.yml` | Đảm bảo least privilege, và CloudTrail phân biệt được function nào đã gọi |
| Layer export ARN (`!ImportValue pandas-layer`) | Script đọc output của stack layer rồi truyền vào | ARN thay đổi mỗi khi layer có version mới, mà CloudFormation không cho cập nhật một export đang được import |
| `sed` thay `\|COMMIT_HASH\|` trong template | `aws cloudformation package` (`Code: ./src`) | Template không bị sửa lúc build. Code không đổi thì S3 key giữ nguyên, nên function cũng không bị update |
| Access key lưu trong GitHub secrets | OIDC + role riêng cho từng môi trường | Không có credential dài hạn nào |

## Chiến lược nhánh & môi trường

| Nhánh | Môi trường | Khi merge |
|---|---|---|
| `dev` | dev | tự deploy |
| `test` | test | tự deploy |
| `main` | prod | cần người duyệt (GitHub Environment protection) |

```
feature/xyz ──PR──▶ dev ──PR──▶ test ──PR──▶ main
```

Mỗi stack có pipeline riêng. Pipeline chỉ chạy khi file của nó thay đổi:

| Pipeline | Kích hoạt khi thay đổi | Deploy |
|---|---|---|
| `infra / private-vpc` | `templates/infrastructure/private-vpc.yml`, `parameters/**` | `<infra>-<env>-private-vpc` |
| `infra / storage` | `.../storage.yml`, `parameters/**` | `<infra>-<env>-storage` |
| `infra / queue` | `.../queue.yml`, `parameters/**` | `<infra>-<env>-queue` |
| `infra / database` | `.../database.yml`, `parameters/**` | `<infra>-<env>-database` |
| `infra / cache` | `.../cache.yml`, `parameters/**` | `<infra>-<env>-cache` |
| `infra / secrets` | `.../secrets.yml`, `parameters/**` | `<infra>-<env>-secrets` |
| `infra / role` | `.../role.yml`, `parameters/**` | `<infra>-<env>-role` |
| `infra / api-gateway` | `.../api-gateway.yml`, `parameters/**` | `<infra>-<env>-api-gateway` |
| `layers` | `templates/layers/**`, `scripts/build-layers.sh` | layers, rồi functions + api (function cần ARN của layer mới) |
| `functions` | `templates/lambda-functions/**` | function, alias, trigger |
| `api` | `templates/api-gateways/**` | resource, function, alias, stage + smoke test |
| `deploy-all` | chỉ chạy thủ công | toàn bộ, theo thứ tự phụ thuộc |

Ghi chú:
- Bảng trên có 8 pipeline infra, tính cả `queue`.
- Tất cả pipeline gọi chung `_deploy.yml`. Logic (map nhánh → môi trường, OIDC, deploy) chỉ nằm ở một chỗ, file pipeline của từng stack chỉ khoảng 30 dòng.
- Mỗi cặp (môi trường, stack) chỉ có một lần deploy tại một thời điểm, còn các stack khác nhau thì chạy song song. Pipeline `api` và job api của `layers` dùng chung một concurrency group nên không bao giờ deploy function cùng lúc.
- `parameters/<env>.json` dùng chung, nên sửa file này sẽ chạy cả 8 pipeline infra. Stack nào không có tham số thay đổi sẽ kết thúc với changeset rỗng (no-op).
- Khi tạo môi trường **mới**, chạy `deploy-all` (scope `all`), vì các stack import export của nhau theo thứ tự `private-vpc → storage → queue → database → cache → secrets → role → api-gateway → layers → functions → api`. Sau đó mới dùng pipeline riêng của từng stack.
- Sửa `scripts/deploy.sh` không kích hoạt pipeline nào. Muốn áp dụng thì chạy `deploy-all`.
- Mỗi pipeline đều có `workflow_dispatch` để chạy lại một stack cho một môi trường bất kỳ.

## Thiết lập lần đầu

### 1. Repo và các nhánh

```bash
git init -b main
git add . && git commit -m "Initial infrastructure"
git branch dev && git branch test
git remote add origin git@github.com:<org>/<repo>.git
git push -u origin main dev test
```

### 2. Bootstrap từng môi trường (credential admin, chạy 1 lần)

```bash
make bootstrap ENV=dev  GITHUB_ORG=<org> GITHUB_REPO=<repo>
make bootstrap ENV=test GITHUB_ORG=<org> GITHUB_REPO=<repo> SINGLETONS=false   # nếu cùng account với dev
make bootstrap ENV=prod GITHUB_ORG=<org> GITHUB_REPO=<repo>                    # account prod riêng (khuyến nghị)
```

Bước này tạo stack `bootstrap-<infra>-<env>`, gồm:
- artifact bucket;
- permissions boundary;
- role `bootstrap/*-github-deploy` (GitHub Actions assume);
- role `bootstrap/*-cfn-exec` (CloudFormation dùng để tạo resource).

Ngoài ra còn có các **resource cấp account**: GitHub OIDC provider, và CloudWatch role cho API Gateway (bắt buộc để stage REST ghi access log). Mỗi account chỉ có một bộ này, nên các môi trường dùng chung account phải đặt `SINGLETONS=false`.

**OIDC subject có ID.** Với một số owner/repo, GitHub gửi claim `sub` dạng `repo:<owner>@<ownerId>/<repo>@<repoId>:environment:<env>` thay vì `repo:<owner>/<repo>:environment:<env>`. Khi đó GitHub Actions báo `Not authorized to perform sts:AssumeRoleWithWebIdentity`. Cách xử lý: truyền thêm ID khi bootstrap.

```bash
make bootstrap ENV=dev GITHUB_ORG=<org> GITHUB_REPO=<repo> GITHUB_OWNER_ID=<id> GITHUB_REPO_ID=<id>
```

Lấy ID bằng `curl https://api.github.com/repos/<org>/<repo>` (`.owner.id`, `.id`), hoặc xem trường `userName` của event `AssumeRoleWithWebIdentity` bị từ chối trong CloudTrail. Trust policy sẽ chấp nhận cả hai dạng.

### 3. GitHub Environments

Vào **Settings → Environments**, tạo `dev`, `test`, `prod`. Mỗi environment cần các **Variables** sau, lấy từ output của bootstrap:

| Variable | Giá trị |
|---|---|
| `AWS_REGION` | ví dụ `ap-southeast-1` |
| `AWS_DEPLOY_ROLE_ARN` | `GitHubDeployRoleArn` |
| `ARTIFACT_BUCKET` | `ArtifactBucketName` |
| `CFN_EXECUTION_ROLE_ARN` | `CloudFormationExecutionRoleArn` |

Với `prod`: bật **Required reviewers**, và giới hạn **Deployment branches** chỉ cho `main`.

### 4. Bảo vệ nhánh

Với `dev`, `test`, `main`: bắt buộc tạo PR, và bắt buộc check **CI / Lint & test** pass.

## Deploy thủ công từ máy local

Cần: AWS CLI v2, `jq`, Python 3, `cfn-lint`, `pytest`.

```bash
pip install cfn-lint pytest boto3 -r templates/layers/common/requirements.txt
make lint test

export ARTIFACT_BUCKET=myapp-dev-artifacts-<account>-<region>
make deploy ENV=dev                 # toàn bộ
make deploy ENV=dev SCOPE=api       # chỉ API (resource, function, alias, stage)
make deploy ENV=dev SCOPE=functions # chỉ lambda-functions (worker SQS…)
make deploy ENV=dev SCOPE=infrastructure STACK=database   # 1 stack infra
```

## Kiểm tra sau khi deploy

```bash
API=$(aws cloudformation describe-stacks --stack-name myapp-dev-api-stage \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text)

curl $API/health                                   # Lambda sống
curl $API/health/deps                              # Kiểm tra Aurora, ElastiCache, EFS
curl -X POST $API/items -H 'Content-Type: application/json' -d '{"name":"book"}'
curl $API/items/<id>                               # Lần 1 đọc từ dynamodb, lần 2 đọc từ cache
curl -X POST $API/jobs -H 'Content-Type: application/json' \
  -d '{"type":"report","payload":{"month":10}}'    # 202 + jobId
aws s3 cp s3://myapp-dev-data-<account>-<region>/jobs/<jobId>.json -   # kết quả do process-job ghi
```

### Luồng SQS

- **Cấu hình xử lý lỗi:**
  - `process-job` trả về `batchItemFailures`, nên SQS chỉ gửi lại những message bị lỗi.
  - Sau `JobsMaxReceiveCount` lần (mặc định 3), message chuyển sang `myapp-<env>-jobs-dlq` và alarm `myapp-<env>-jobs-dlq-not-empty` chuyển sang ALARM.
- **Đưa message trong DLQ về lại queue chính:**
  ```bash
  aws sqs start-message-move-task --source-arn <JobsDeadLetterQueueArn>
  ```
  Lệnh này nên chạy sau khi đã sửa lỗi.
- **Trigger gắn trên alias `live`:**
  - Rollback alias thì phần consume queue cũng rollback theo.
  - Muốn tạm dừng xử lý thì đặt `Enabled: false` cho `SqsTrigger`.
- **Tham số có thể chỉnh:**

  | Tham số | Mặc định | Nằm trong |
  |---|---|---|
  | `WorkerBatchSize` | 10 | `process-job/lambda_function.yml` |
  | `WorkerMaxConcurrency` | 5 | `process-job/lambda_function.yml` |
  | `WorkerTimeout` | 60s | `process-job/template.yml` |
  | `JobsVisibilityTimeout` | 360s | `queue.yml` |

  `JobsVisibilityTimeout` phải ≥ 6 × `WorkerTimeout`. Muốn đặt giá trị khác cho từng môi trường thì thêm key vào `parameters/<env>.json`.
- **Viết worker idempotent:** SQS giao message *ít nhất một lần*, nên cùng một job có thể chạy hai lần. Worker này ghi theo key `jobs/<jobId>.json`, nên nếu job chạy lại thì chỉ ghi đè, không tạo bản trùng.

**Rollback một function:** trỏ alias `live` về version cũ.

```bash
aws lambda update-alias --function-name myapp-dev-fn-items-post --name live --function-version <N>
```

## Mô hình phân quyền (IAM)

```
GitHub Actions ──OIDC──▶ bootstrap/<infra>-<env>-github-deploy   (change set, upload artifact, tạo API deployment, PassRole)
                                   │ --role-arn
                                   ▼
                         bootstrap/<infra>-<env>-cfn-exec          (quyền tạo resource, giới hạn theo dịch vụ / tên / region)
                                   │ chỉ được tạo role CÓ boundary
                                   ▼
                         <infra>-<env>-fn-*, <infra>-<env>-vpc-flowlogs   ≤  boundaries/<infra>-<env>-boundary
```

1. **Tách người gọi và người thực thi.** Role của GitHub không có quyền tạo resource. Nó chỉ ra lệnh cho CloudFormation.
2. **`cfn-exec` có quyền giới hạn.** Lambda, layer, log group, S3, DynamoDB, SQS, CloudWatch alarm, secret, IAM bị giới hạn theo tên `<infra>-<env>-*`. Các dịch vụ có ID tự sinh (EC2, RDS, ElastiCache, EFS, API Gateway) bị giới hạn theo region.
3. **Permissions boundary.** `cfn-exec` chỉ tạo được role khi role đó có gắn boundary, và role/policy của bootstrap (path `/bootstrap/`) bị Deny tường minh. Vì vậy pipeline không thể tự nâng quyền.

**Dùng chung policy, không dùng chung role.** `templates/infrastructure/role.yml` export các managed policy nhỏ: `ItemsTableReadWritePolicy`, `ItemsTableReadOnlyPolicy`, `DataBucketReadWritePolicy`, `SecretsReadPolicy`, `EfsClientPolicy`, `JobsQueueSendPolicy`, `JobsQueueConsumePolicy`. Mỗi `template.yml` tự tạo role cho function của mình và chỉ gắn những policy cần dùng:

| Function | Policy |
|---|---|
| `health-get` | `AWSLambdaBasicExecutionRole` (không vào VPC) |
| `health-deps-get` | VPC access, `SecretsReadPolicy`, `EfsClientPolicy` |
| `items-post` | VPC access, `ItemsTableReadWritePolicy`, `DataBucketReadWritePolicy`, `EfsClientPolicy` |
| `jobs-post` | VPC access, `JobsQueueSendPolicy` |
| `process-job` | VPC access, `JobsQueueConsumePolicy`, `DataBucketReadWritePolicy` |
| `items-id-get` | VPC access, `ItemsTableReadOnlyPolicy` |

## Khác biệt giữa các môi trường (`parameters/*.json`)

| | dev | test | prod |
|---|---|---|---|
| Aurora ACU | 0.5–2 | 0.5–4 | 1–16 + reader |
| Backup Aurora | 1 ngày | 3 ngày | 14 ngày |
| ElastiCache | t4g.micro × 1 | t4g.small × 1 | r7g.large × 2 (Multi-AZ) |
| DynamoDB PITR / EFS backup | tắt | PITR bật | bật cả hai |
| Deletion protection | tắt | tắt | bật |
| Log retention | 7 ngày | 14 ngày | 90 ngày |

Mọi stack đều nhận toàn bộ file parameter. `aws cloudformation deploy` chỉ chuyển những key mà template đó có khai báo.

## Lưu ý vận hành

- **Thứ tự xoá ngược với thứ tự tạo.** Không xoá được một stack khi còn stack khác đang `!ImportValue` output của nó. Thứ tự xoá: stage → function/alias (kể cả `fn-process-job*`) → `api-*` → layer → `api-gateway` → `role` → `secrets` → `cache` → `database` → `queue` → `storage` → `private-vpc`.
- **Dữ liệu được giữ lại.** S3 bucket và DynamoDB table dùng `Retain`, Aurora dùng `Snapshot`. Bucket và table có tên cố định, nên sau khi xoá stack phải xoá tay chúng thì mới tạo lại được.
- **Thêm dịch vụ AWS mới.** Phải bổ sung quyền vào `CfnExecInfraPolicy` **và** `WorkloadPermissionsBoundary` trong `bootstrap/bootstrap.yml`, rồi deploy lại bootstrap bằng credential admin.
- **Production.** Nên thêm RDS Proxy, WAF và custom domain cho API, cùng CloudWatch alarms.
- **Chi phí.** Interface endpoint của Secrets Manager, Aurora và ElastiCache tính tiền theo giờ, kể cả ở dev.
