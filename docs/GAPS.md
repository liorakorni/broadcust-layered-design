# Known gaps and follow-ups

Things that work today but are not where they should end up. Each entry says what
the current state is, why it matters, and what closing it involves — so the
decision to defer stays a decision rather than becoming an accident.

None of these block the dev deploy. One is a credential-handling inconsistency
worth closing before this service carries real traffic, and two are about how
little stands between a laptop and production.

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

## 4. No CI

**State.** The repo is now on GitHub at `liorakorni/broadcust-layered-design`
(private), with `main` and `dev` both pushed and tracking. What is still missing
is anything that runs on a push: no workflow, no test run, no deploy.

So the 222 tests only ever run when someone remembers to run them, and the
branch-to-stage rule that `scripts/deploy.sh` enforces — `dev` to dev, `main` to
live — is enforced only on the laptop doing the deploying.

**Why it matters.** Several of the defects found by deploying were invisible to
the test suite but would have been caught by running it anywhere other than the
machine that wrote the code — the missing `requests` dependency is the clearest
example, since it was present in the developer's venv and nowhere else.

**To close.** A workflow running `pytest` on push and on pull requests is the
cheap half, and worth doing regardless. Deploying from CI is the expensive half:
it needs AWS credentials that are not root (see gap 2) and a decision about
whether `main` should deploy to live automatically or on a tag.

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

---

## 6. Text fitting compensates for box geometry rather than correcting it

**Closed, with a residual.** The first full live run was rejected by REVIEW with
five `TEXT_OVERFLOW` issues whose ratios clustered at 1.90, 1.93, 1.95, 2.02 and
2.10 — one mechanism, not scattered bad guesses. EXTRACT_SCENE sizes every box
for exactly one line, so a box a few percent too narrow for Heebo wraps to a
second line and the height doubles. A 7% width underestimate produced a 110%
height overflow.

ASSEMBLE now measures each block in the browser that will render it and reduces
`fontSize` to the largest size that fits, bounded at 75% of the requested size.
The next run came back with zero render problems and was approved without a
correction round, reducing the headline from 89px to 79px and the subtitle from
32px to 31px.

**What remains.** The fit treats the symptom. EXTRACT_SCENE still sizes boxes for
a single line and still estimates widths from a concept drawn in another
typeface, so every design arrives slightly too large and is quietly shrunk to
fit. That is fine while the error stays small, and the floor keeps it honest
when it does not — text that cannot fit at 75% is left and reported rather than
shrunk into illegibility. But it means the delivered typography is systematically
a little smaller than the design intended, and nothing currently reports how
often the floor is being hit.

**To close.** Two things worth doing, neither urgent. Ask EXTRACT_SCENE for box
heights that allow for wrapping, so the fit has less to absorb. And surface the
`fitted` record ASSEMBLE already persists — it holds the original and fitted size
per layer — so a run of designs can be checked for layers pinned at the floor,
which is the signal that the geometry is wrong rather than merely tight.

---

## 7. A correction round after an overlay repair has no repair budget left

**State.** SPEC section 4 bounds overlay repairs to "two per job, not two per
task retry", and the implementation honours that literally. The live run spent
both repairs on attempt 1, passed validation, rendered, and was then sent back
for a correction round. Attempt 2 regenerated the overlay from the concept, that
fresh overlay was rejected by the region check, and there was no repair budget
left — so the job ended there.

**Why it matters.** The interaction, not either bound on its own, is the
problem: a correction round regenerates assets, and regenerated assets are about
as likely to need a repair as the originals were. Any job that uses its repairs
early and later takes a correction round is therefore likely to die, which makes
the correction loop much less useful than it looks.

This is faithful to the spec as written, so it is a question about the spec
rather than a defect in the code.

**To close.** Decide which the spec means: a per-job bound on paid repair calls
(current reading, cheap, fragile), or a per-attempt bound with a per-job ceiling
(more forgiving, more expensive). Needs the requester's input.

---

## 8. Agent session ownership trusts every action group on the agent

**State.** An agent call has no trusted caller context. Ownership comes from the
`ownerScope` session attribute, which the application backend sets in
`InvokeAgent`. The model cannot write session attributes, and `ownerScope` is not
an Action Group parameter, so a prompt cannot claim another tenant's jobs.

**Why it matters.** An action group Lambda can return modified session
attributes, and the agent carries them forward. Every action group attached to
the agent is therefore trusted not to rewrite `ownerScope`. This service returns
them unmodified (`lib.bedrock.envelope`); it cannot vouch for the others.

**To close.** Attach only action groups you control, or replace the attribute
with a value the Lambda can verify, such as a signed token with a rotated key.

The same attribute has to be set by `broadcust-bedrock-api`, which does not do so
yet. Until it does, every agent call fails closed with `UNAUTHORIZED`.

---

## 9. No role is granted direct invoke on the API function

**State.** The backend is meant to read complete results by invoking the API
function directly, and `trustedCallerContext` is trusted only because IAM limits
who can do that. No backend role ARN has been supplied, so the deploy grants
`lambda:InvokeFunction` to nothing but the Bedrock agent (when configured).

**Why it matters.** Until a role is granted, only account administrators can use
the direct path. Granting it too broadly is worse than not granting it: any
principal that can invoke can claim any `ownerScope`.

**To close.** Get the backend's execution role ARN and add a scoped
`AWS::Lambda::Permission` or a policy on that role.

---

## 10. Retention is an exclusion from deletion, not a durable copy

**State.** Saved designs and their dependencies are tagged `lifecycle=retained`
and the job record loses its expiry. They stay in the artifact bucket, which is
the same bucket as the temporary files.

Three limits:

- Objects written before the tagging shipped carry no tag, so the new lifecycle
  rule never expires them. Delete old dev jobs by hand if that matters.
- The idempotency claim record still expires at 30 days. After that, reusing a
  `requestId` starts a new, paid job. The saved design stays retrievable by its
  `jobId`.
- The bucket has no versioning or replication, so a saved design has one copy.

**To close.** If saved designs need stronger durability, copy them to dedicated
storage at FINALIZE and drop the tags.

---

## 11. The service is Python; the spec says Node.js 24 ES modules

**State.** SPEC section 3 and the file list in section 8 specify Node.js 24 ES
modules and `.mjs` files such as `prototype.mjs`, `pipeline.mjs` and
`reference-policy.mjs`. The service is Python on a container-image Lambda. The
requester was asked and chose to keep Python; the filenames are treated as
illustrative (`lib/reference_policy.py` implements `deriveReferencePolicy`).

**To close.** Only if the requester later requires the Node runtime, which would be
a rewrite rather than a change.

---

## 12. Phase A and Phase B acceptance gates have not been produced

**State.** The 2026-10-19 spec (section 0) makes a local prototype and its evidence
the first deliverable: `node prototype.mjs`, a labelled reference/concept/HTML
comparison, a human visual review, verification inside the real Broadcust editor,
and at least three materially different brief/reference pairs at `designFreedom`
0, 50 and 100 (plus 25 and 75 on one). The production pipeline was built and
deployed first, so none of that evidence exists.

What does exist: unit tests, an opt-in live integration test per band
(`tests/test_integration.py`) and live runs of the deployed workflow.

**To close.** Run the three-pair matrix against the deployed service, record
policy evidence and a human review per case, and run the editor import. Mark the
editor gate pending until it has been executed; do not claim it was tested.

