# Known gaps and follow-ups

Things that work today but are not where they should end up. Each entry says what
the current state is, why it matters, and what closing it involves — so the
decision to defer stays a decision rather than becoming an accident.

None of these block the dev deploy. Two of them block a second developer, and one
is a credential-handling inconsistency worth closing before this service carries
real traffic.

---

## 1. Corporate TLS interception is handled by an untracked certificate

**State.** This network runs a Cato Networks proxy that re-signs every TLS
certificate. Anything verifying against the public CA set fails with
`self-signed certificate in certificate chain`.

Two independent workarounds are in place, and neither is reproducible from a
fresh clone:

| Where | Workaround | Tracked in git? |
|---|---|---|
| Docker build | `assets/ca-certificates/*.crt` copied to the image's trust anchors | **No** — `.gitignore:15` excludes `*.crt` |
| Local test runs | Cato root CA appended to `.venv/.../certifi/cacert.pem` | **No** — `.venv/` is ignored, and the edit is lost on venv rebuild |

**Why it matters.** A teammate cloning this repo on the same network cannot build
the image or run the Cloudinary integration test until they extract the CA from
their own keychain. Nothing tells them that — they get a TLS error that reads
like a credentials problem, which is exactly how it presented here and cost real
time to diagnose.

Excluding the certificate from git is the right call: it is machine- and
network-specific, and on a network without interception the Docker step is
correctly a no-op. The gap is the missing *instructions*, not the missing file.

**Worth knowing.** The interception is selective. `api.openai.com` passes
straight through; `api.cloudinary.com` is intercepted. So "OpenAI works" is not
evidence that TLS is fine.

**To close.** A `scripts/trust-corporate-ca.sh` that extracts the root CA from
the macOS keychain into `assets/ca-certificates/` and appends it to the venv's
certifi bundle, plus a README line pointing at it. Roughly half an hour. The
extraction command already exists in this conversation's history.

**Separate point: the venv patch is a liability.** Appending to certifi's bundle
widens trust for *every* Python package in that venv, and silently reverts on
`pip install --force-reinstall certifi`. A wrapper that builds a combined bundle
at `.venv/ca-bundle.pem` would be cleaner, except the Cloudinary SDK hardcodes
`ca_certs=certifi.where()` and ignores `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE` and
`CURL_CA_BUNDLE` — which is why the bundle approach was tried first and failed.
Patching certifi is the only option that works without monkeypatching the SDK.

---

## 2. Deploys run as the AWS account root

**State.** `AWS_PROFILE=broadcust` resolves to
`arn:aws:iam::816069163425:root`.

**Why it matters.** Root has unrestricted access to every service in the account
and cannot be scoped, so the least-privilege work in `serverless.yml` — one role
per function, S3 access confined to the artifact prefix, Secrets Manager access
confined to a single secret ARN — protects the *running* service but not the
deploy path. Root credentials also cannot be revoked granularly: if they leak,
the remedy is rotating the account root. AWS's own guidance is to use root only
for the handful of tasks that require it, and deployment is not one of them.

This is pre-existing and not specific to this service; the sibling repos deploy
the same way.

**To close.** An IAM user or role with a deployment policy covering
CloudFormation, Lambda, ECR, IAM role creation, DynamoDB, S3 and Step Functions,
then repoint the `broadcust` profile at it. An hour or two, mostly spent
narrowing the policy. Best done once for all the repos rather than per-service.

---

## 3. `OPENAI_API_KEY` is still a plain environment variable

**State.** Cloudinary credentials moved to Secrets Manager (commit `1528c32`),
but the OpenAI key is still read from `config.dev.json` and injected as a Lambda
environment variable by `serverless.yml`.

**Why it matters.** It is now the only credential handled the old way, and the
asymmetry is the problem — someone reading the config reasonably concludes that
environment variables are the convention here. Concretely, the key is visible in
the Lambda console, in `GetFunctionConfiguration` for anyone with read access, in
CloudFormation stack parameters, in `config.dev.json` on every developer's
laptop, and in the output of `npx serverless print` (which echoed it in full
during this build). Rotating it requires a redeploy rather than taking effect on
the next cold start.

**To close.** The mechanism already exists and is tested — `lib/secrets.py` is
generic, and `lib/media.py:_credentials()` shows the pattern, including the
environment fallback that keeps local tests working without an AWS role. The work
is:

1. Decide whether the key joins the existing `Cloudinery` secret or gets its own.
   Its own is better: the two have different rotation cadences and different
   consumers, and the IAM grant stays scoped per credential.
2. Add `OPENAI_SECRET_ARN` to `conf.py` alongside `CLOUDINARY_SECRET_ARN`.
3. Resolve it in `lib/providers/image.py` and `lib/providers/vision.py`, which
   both read `conf.OPENAI_API_KEY` when building their client.
4. Extend the `StageRole` Secrets Manager grant to the second ARN.
5. Drop `OPENAI_API_KEY` from `serverless.yml` and `config.dev.json`.

Under an hour, and the tests in `tests/test_secrets.py` already cover the
resolution logic itself.

**Note on `STAGE_REQUIRED`.** It currently lists `OPENAI_API_KEY`, so that
constant and `conf.validate_media()`'s either-or logic need the same treatment —
see how `validate_media` accepts a secret ARN *or* direct values.

---

## 4. No git remote

**State.** The repo has local commits on `main` and `dev` and no remote. `gh` is
not installed on this machine, so the remote was never created.

**Why it matters.** The entire service exists on one laptop. There is no backup,
no review path, and no CI.

**To close.** Install `gh`, authenticate, `gh repo create`, push both branches.
Ten minutes.

---

## 5. The container image is 3.04 GB

**State.** Within Lambda's 10 GB limit, but large. `playwright install chromium`
pulls both full Chromium and the headless shell, and only the shell is used.

**Why it matters.** Cold starts are slow, and every deploy pushes a large image.
Not a correctness problem, and the stage Lambda has a 240 s timeout with plenty
of headroom — but worth revisiting if cold starts show up in practice.

**To close.** Install only `chromium-headless-shell`, or move Chromium into a
Lambda layer so application changes stop rebuilding it. Measure the cold start
first; this is an optimisation without a demonstrated problem.
