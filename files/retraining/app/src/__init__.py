# src/ package — mirror of /root/thesis-infra/src/ for container build context.
# Following the EC#11 + EC#17 pattern used in files/fastapi/src/ and files/baseline-refresh/src/.
# Acceptable temporary duplication until mlflow.pytorch.log_model(..., code_paths=['src'])
# is adopted at training time, which would embed src/ into the model artifact.
