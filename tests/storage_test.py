"""Object storage wiring, checked against a stub.

    python tests/storage_test.py

No bucket needed and none contacted. What is worth pinning here is the wiring
rather than S3 itself: which environment variables are read, what key an
upload lands under, what URL comes back for it, and that a storage failure is
reported rather than raised -- the sheet is already drawn and on disk by then,
so an outage must not read as a failed plan.
"""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fails = 0
def check(name, ok, detail=""):
    global fails
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{'' if ok or not detail else f' -- {detail}'}")
    if not ok: fails += 1

for k in list(os.environ):
    if k.startswith(("S3_", "LINODE_", "AWS_")):
        del os.environ[k]

import upload
print("== configuration ==")
check("unconfigured is detected", not upload.is_configured())
check("an unconfigured upload returns None, does not raise",
      upload.upload_file("/etc/hosts") is None)

os.environ.update({
    "S3_ENDPOINT": "https://eu-central-1.linodeobjects.com",
    "S3_REGION": "eu-central-1",
    "S3_BUCKET": "autoplan",
    "LINODE_ACCESS_KEY_ID": "AKIAEXAMPLE",
    "LINODE_SECRET_ACCESS_KEY": "secret",
})
check("configured is detected", upload.is_configured())
check("bucket read from S3_BUCKET", upload.bucket_name() == "autoplan")

print("\n== the URL handed back ==")
url = upload.public_url("survey_plans/plan-1.zip")
check("built from the endpoint and bucket",
      url == "https://eu-central-1.linodeobjects.com/autoplan/survey_plans/plan-1.zip", url)

print("\n== falls back to the AWS names ==")
del os.environ["LINODE_ACCESS_KEY_ID"], os.environ["LINODE_SECRET_ACCESS_KEY"]
os.environ["AWS_ACCESS_KEY"] = "a"; os.environ["AWS_SECRET_KEY"] = "b"
check("a deployment with AWS_* keys still works", upload.is_configured())

print("\n== what actually gets sent ==")
calls = []
class Stub:
    def upload_file(self, path, bucket, key, ExtraArgs=None):
        calls.append({"path": path, "bucket": bucket, "key": key, "extra": ExtraArgs})
    def delete_object(self, Bucket, Key):
        calls.append({"delete": Key})
upload._client = Stub()

got = upload.upload_file("/etc/hosts", folder="survey_plans", file_name="plan-1.zip")
c = calls[-1]
check("bucket", c["bucket"] == "autoplan", c["bucket"])
check("key is folder/name", c["key"] == "survey_plans/plan-1.zip", c["key"])
check("public-read so the link works for whoever it is shared with",
      c["extra"]["ACL"] == "public-read", str(c["extra"]))
check("content type is set", "ContentType" in c["extra"], str(c["extra"]))
check("returns the public URL",
      got == "https://eu-central-1.linodeobjects.com/autoplan/survey_plans/plan-1.zip", str(got))

upload.upload_file("/etc/hosts", folder="", file_name="loose.txt")
check("no folder means a bare key", calls[-1]["key"] == "loose.txt", calls[-1]["key"])

print("\n== a storage failure is reported, not raised ==")
class Boom:
    def upload_file(self, *a, **k): raise RuntimeError("bucket on fire")
upload._client = Boom()
check("returns None instead of raising", upload.upload_file("/etc/hosts") is None)

print("\n== AWS proper, with no custom endpoint ==")
del os.environ["S3_ENDPOINT"]
check("falls back to the AWS URL shape",
      upload.public_url("k/f.zip") == "https://autoplan.s3.eu-central-1.amazonaws.com/k/f.zip",
      upload.public_url("k/f.zip"))

print(f"\n{fails} failure(s)" if fails else "\nall storage checks pass")
sys.exit(1 if fails else 0)
