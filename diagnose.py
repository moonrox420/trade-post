import os
import subprocess

# Let's run a dry import check of drox_trade_post.py on their system to see the exact error it produces right now
target_path = r"C:\Users\droxa\trade_post\drox_trade_post.py"
if os.path.exists(target_path):
    try:
        # Run python -m py_compile to check for syntax/import errors
        completed_process = subprocess.run(
            ["python", "-m", "py_compile", target_path],
            capture_output=True,
            text=True,
            timeout=10
        )
        print("Compile stdout:", completed_process.stdout)
        print("Compile stderr:", completed_process.stderr)
        print("Compile exit code:", completed_process.returncode)
    except Exception as e:
        print("Execution failed:", e)
else:
    print(f"File {target_path} not found")