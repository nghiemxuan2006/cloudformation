# myapp – Serverless API trên AWS (CloudFormation)

Client → **API Gateway (HTTP API)** → **Lambda** (trong private subnet của VPC) → EFS, Aurora PostgreSQL Serverless v2, ElastiCache (Valkey), DynamoDB, S3, Secrets Manager.

VPC **không có NAT Gateway**. Lambda gọi các dịch vụ AWS hoàn toàn qua đường private:

| Dịch vụ | Cách Lambda kết nối |
|---|---|
| S3, DynamoDB | Gateway VPC Endpoint (miễn phí) |
| Secrets Manager | Interface VPC Endpoint (PrivateLink) |
| Aurora, ElastiCache, EFS | Đặt trong cùng private subnet, kiểm soát bằng Security Group |

> Sơ đồ vẽ "Endpoint" cho ElastiCache, nhưng ElastiCache không có VPC endpoint kiểu PrivateLink cho data plane. Cluster được đặt trực tiếp trong private subnet, và Lambda kết nối tới nó qua Security Group (port 6379, có TLS).

## Cấu trúc thư mục

```
.
├── bootstrap/bootstrap.yaml      # Chạy 1 lần/môi trường: artifact bucket, OIDC, IAM roles cho CI/CD
├── infra/
│   ├── main.yaml                 # Root stack – gọi các nested stack
│   ├── stacks/
│   │   ├── network.yaml          # VPC, subnets, SG, VPC endpoints, flow logs
│   │   ├── storage.yaml          # S3, DynamoDB, EFS (+ mount targets, access point)
│   │   ├── database.yaml         # Aurora PostgreSQL Serverless v2 (secret do RDS quản lý)
│   │   ├── cache.yaml            # ElastiCache Valkey (TLS)
│   │   ├── security.yaml         # IAM managed policies dùng chung, role của workload, app secret
│   │   └── compute.yaml          # Lambda, HTTP API
│   └── parameters/
│       ├── dev.json              # Cấu hình riêng cho từng môi trường
│       ├── test.json
│       └── prod.json
├── src/api/                      # Code Lambda (Python 3.12, arm64)
├── tests/                        # Unit test
├── scripts/
│   ├── build-lambda.sh           # Build package Lambda vào build/lambda
│   └── deploy.sh                 # package + deploy 1 môi trường
├── .github/workflows/
│   ├── ci.yml                    # PR: cfn-lint + pytest + build
│   └── deploy.yml                # Push vào nhánh: deploy lên môi trường tương ứng
└── Makefile
```

**Nguyên tắc:** chỉ có **một bộ template** dùng chung. Mọi khác biệt giữa các môi trường (CIDR, kích cỡ DB/cache, số replica, deletion protection, log retention…) đều nằm trong `infra/parameters/<env>.json`, không nằm trong `Conditions` theo tên môi trường.

## Chiến lược nhánh & môi trường

| Nhánh | Môi trường | Stack | Ghi chú |
|---|---|---|---|
| `dev` | dev | `myapp-dev` | Tự động deploy khi merge |
| `test` | test | `myapp-test` | Tự động deploy khi merge (QA/UAT) |
| `main` | prod | `myapp-prod` | Cần reviewer duyệt (GitHub Environment protection) |

Quy trình:

```
feature/xyz ──PR──▶ dev ──PR──▶ test ──PR──▶ main
                     │            │            │
                  deploy dev   deploy test  deploy prod (cần duyệt)
```

- Mỗi PR chạy `ci.yml` (lint, test, build). Bật branch protection để bắt buộc CI pass.
- Khi merge, `deploy.yml` xác định môi trường theo tên nhánh, assume IAM role của **đúng môi trường đó** qua OIDC rồi chạy `scripts/deploy.sh`.
- Muốn thêm môi trường (ví dụ `staging`): thêm `infra/parameters/staging.json`, thêm `staging` vào `AllowedValues` trong `main.yaml` và `bootstrap.yaml`, thêm dòng mapping trong `deploy.yml`, rồi bootstrap môi trường mới.

## Thiết lập lần đầu

### 1. Tạo repo và các nhánh

```bash
git init -b main
git add . && git commit -m "Initial infrastructure"
git branch dev && git branch test
git remote add origin git@github.com:<org>/<repo>.git
git push -u origin main dev test
```

### 2. Bootstrap từng môi trường (dùng credential admin, chạy 1 lần)

Bước này tạo stack `bootstrap-<project>-<env>` gồm: artifact bucket, GitHub OIDC provider, permissions boundary, role `bootstrap/*-github-deploy` (GitHub Actions assume) và role `bootstrap/*-cfn-exec` (CloudFormation dùng để tạo resource). Xem mục [Mô hình phân quyền](#mô-hình-phân-quyền-iam).

```bash
make bootstrap ENV=dev  GITHUB_ORG=<org> GITHUB_REPO=<repo>
make bootstrap ENV=test GITHUB_ORG=<org> GITHUB_REPO=<repo> CREATE_OIDC=false   # nếu cùng account với dev
make bootstrap ENV=prod GITHUB_ORG=<org> GITHUB_REPO=<repo>                     # account prod riêng (khuyến nghị)
```

> Mỗi AWS account chỉ được có **một** GitHub OIDC provider. Nếu nhiều môi trường dùng chung account, chỉ môi trường đầu tiên để `CREATE_OIDC=true`.

### 3. Tạo GitHub Environments

Vào **Settings → Environments**, tạo `dev`, `test`, `prod`. Với mỗi environment, thêm các **Variables** sau, lấy giá trị từ output của bootstrap stack:

| Variable | Giá trị |
|---|---|
| `AWS_REGION` | ví dụ `ap-southeast-1` |
| `AWS_DEPLOY_ROLE_ARN` | `GitHubDeployRoleArn` |
| `ARTIFACT_BUCKET` | `ArtifactBucketName` |
| `CFN_EXECUTION_ROLE_ARN` | `CloudFormationExecutionRoleArn` |

Với `prod`: bật **Required reviewers** và giới hạn **Deployment branches** chỉ cho `main`. Tương tự, giới hạn `dev` chỉ cho nhánh `dev` và `test` chỉ cho nhánh `test`.

Trust policy của role chỉ chấp nhận token có `sub = repo:<org>/<repo>:environment:<env>`. Vì vậy job deploy dev không thể assume role prod.

### 4. Bảo vệ nhánh

Với `dev`, `test`, `main`: bắt buộc tạo PR, bắt buộc check **CI / Lint & test** pass, và không cho push thẳng.

## Deploy thủ công từ máy local

Cần: AWS CLI v2, `jq`, Python 3, `cfn-lint`.

```bash
pip install cfn-lint pytest boto3 -r src/api/requirements.txt
make lint test

export ARTIFACT_BUCKET=myapp-dev-artifacts-<account>-<region>
make plan   ENV=dev     # chỉ tạo change set để xem trước thay đổi
make deploy ENV=dev
```

## Kiểm tra sau khi deploy

```bash
API=$(aws cloudformation describe-stacks --stack-name myapp-dev \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text)

curl $API/health                       # Lambda sống
curl $API/health/deps                  # Kiểm tra Aurora, ElastiCache, EFS
curl -X POST $API/items -d '{"name":"book"}'   # Ghi vào DynamoDB, S3 và log EFS
curl $API/items/<id>                   # Lần 1 đọc từ dynamodb, lần 2 đọc từ cache
```

## Mô hình phân quyền (IAM)

```
GitHub Actions ──OIDC──▶ bootstrap/<p>-<env>-github-deploy   (chỉ: CreateChangeSet/Execute, upload artifact, PassRole)
                                   │ --role-arn
                                   ▼
                         bootstrap/<p>-<env>-cfn-exec          (quyền tạo resource, giới hạn theo dịch vụ / tên / region)
                                   │ chỉ được tạo role CÓ boundary
                                   ▼
                         <p>-<env>-api-fn, <p>-<env>-vpc-flowlogs …   ≤  boundaries/<p>-<env>-boundary
```

Có 3 lớp bảo vệ:

1. **Tách người gọi và người thực thi.** Role của GitHub không có quyền tạo resource. Nó chỉ ra lệnh cho CloudFormation, và CloudFormation dùng `cfn-exec` để tạo resource.
2. **`cfn-exec` có quyền giới hạn, không dùng `AdministratorAccess`:**
   - Lambda, log group, S3, DynamoDB, secret, IAM role/policy bị giới hạn theo tên `<project>-<env>-*`, nên dev không đụng được tài nguyên của test.
   - Các dịch vụ có ID tự sinh (EC2/VPC, RDS, ElastiCache, EFS, API Gateway) được giới hạn theo region.
3. **Permissions boundary (chống leo thang quyền).**
   - `cfn-exec` chỉ được `CreateRole`/`AttachRolePolicy`/`PutRolePolicy` khi role đó có gắn `boundaries/<p>-<env>-boundary`.
   - Dù template khai báo `AdministratorAccess` cho một role, quyền thực tế vẫn chỉ bằng phần giao giữa policy đó và boundary.
   - Role/policy của bootstrap nằm ở path `/bootstrap/` và bị Deny tường minh, nên pipeline không thể tự nâng quyền của chính mình.

### Tái sử dụng role/policy (`infra/stacks/security.yaml`)

- **Dùng chung policy, không dùng chung role.** Mỗi năng lực là một managed policy nhỏ: `items-table-rw`, `items-table-ro`, `data-bucket-rw`, `secrets-read`, `efs-client`.
- **Mỗi workload có role riêng**, ghép từ các policy trên. Nếu nhiều Lambda dùng chung một role, function nào cũng có quyền của mọi function khác, và CloudTrail không phân biệt được ai gọi.
- **ARN được export** dưới dạng `<project>-<env>:<Tên>`. Stack khác trong cùng account/region có thể dùng lại như sau:

```yaml
WorkerRole:
  Type: AWS::IAM::Role
  Properties:
    RoleName: !Sub myapp-${Environment}-worker-fn          # phải khớp <project>-<env>-*
    PermissionsBoundary: !ImportValue myapp-dev:PermissionsBoundaryArn
    AssumeRolePolicyDocument: { ... lambda.amazonaws.com ... }
    ManagedPolicyArns:
      - !Sub arn:${AWS::Partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
      - !ImportValue myapp-dev:ItemsTableReadOnlyPolicyArn
      - !ImportValue myapp-dev:AppSecretsReadPolicyArn
```

> Lưu ý khi dùng export: CloudFormation không cho sửa hoặc xoá một export khi còn stack khác đang `!ImportValue` nó. Vì vậy chỉ export những thứ ổn định như ARN của policy/role. Nếu cần ít ràng buộc hơn, có thể ghi ARN vào SSM Parameter Store (`/myapp/dev/iam/...`) và đọc qua `{{resolve:ssm:...}}`.

`FlowLogRole` vẫn nằm trong `network.yaml`. Đây là service role chỉ phục vụ log group của VPC flow logs, và chuyển nó sang `security.yaml` sẽ tạo phụ thuộc vòng: security → storage → network → security.

## Khác biệt giữa các môi trường

| | dev | test | prod |
|---|---|---|---|
| Aurora ACU | 0.5–2 | 0.5–4 | 1–16 + reader |
| Backup Aurora | 1 ngày | 3 ngày | 14 ngày |
| ElastiCache | t4g.micro × 1 | t4g.small × 1 | r7g.large × 2 (Multi-AZ) |
| DynamoDB PITR / EFS backup | tắt | PITR bật | bật cả hai |
| Deletion protection | tắt | tắt | bật |
| Log retention | 7 ngày | 14 ngày | 90 ngày |

## Lưu ý vận hành

- **Tài nguyên có dữ liệu:** S3 bucket và DynamoDB table dùng `DeletionPolicy: Retain`. Aurora dùng `Snapshot`. Khi xoá stack, dữ liệu vẫn được giữ lại và phải dọn thủ công.
- **Mật khẩu DB** do RDS tự sinh và lưu trong Secrets Manager (`ManageMasterUserPassword`), không xuất hiện trong template hay pipeline.
- **Production:** nên thêm RDS Proxy (connection pooling cho Lambda), AWS WAF/custom domain cho API và CloudWatch alarms.
- **Thêm dịch vụ mới** (ví dụ SQS): cần thêm quyền cho dịch vụ đó vào `CfnExecInfraPolicy` (để tạo được resource) **và** vào `WorkloadPermissionsBoundary` (để role được dùng), rồi deploy lại bootstrap bằng credential admin. Đây là chủ đích thiết kế: mở thêm quyền phải qua người quản trị, không tự mở được qua PR.
- **S3 bucket và DynamoDB table có tên cố định** (cần để giới hạn IAM theo tên). Vì chúng dùng `Retain`, sau khi xoá stack phải xoá tay bucket/table cũ thì mới tạo lại được.
- **Chi phí:** Interface endpoint của Secrets Manager (2 AZ), Aurora và ElastiCache tính tiền theo giờ, kể cả ở dev. Nên xoá stack dev/test khi không dùng.
