# Job c419e88204e1423fbc761304a0333b54 (diagnostic case)

Real model output used as regression fixtures.

* `copy.json` - the approved copy manifest of the job (includes `business.name` = FIT HOME).
* `analysis.json` - the reference analysis produced by the policy 2.0 prompt
  (content-neutral anchors, planned copy placements) for the job's reference image.
* `verdict-attempt{1,2,3}.json` - the raw concept-review verdict (`concept_review_v2`)
  the real review model gave for each of the job's three historical concept attempts
  when re-evaluated under policy 2.0 / `reviewStrictness: balanced`.

The historical reports of the job (policy 1.0) are untouched in its S3 prefix; these
reevaluations were produced locally and are stored here, separately.

Outcome under balanced review: attempt 1 and 3 are missing `business.name` (blocking
content); attempt 2 has all copy and only warnings (photo colour treatment, business
name placed lower than planned, a subtle added rule).
