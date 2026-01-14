from typing import Dict, List, Optional
from .adb_service import run_adb_once
from .utils import download_temp_file

def run_adb_sequence(serial: str, command_text: str) -> Dict[str, object]:
    def get_installed_packages(target_serial: str) -> set:
        res = run_adb_once(target_serial, "shell pm list packages")
        if res.get("code") != 0:
            return set()
        out = res.get("stdout", "")
        packages = set()
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                packages.add(line.replace("package:", ""))
        return packages

    # --- XỬ LÝ LỆNH ĐẶC BIỆT: net-push ---
    # Cú pháp: net-push <URL> <DESTINATION_PATH>
    import shlex
    if command_text.strip().startswith("net-push"):
        parts = shlex.split(command_text)
        print(f"[agent] net-push command: {command_text}")
        if len(parts) >= 3:
            url = parts[1]
            dest = parts[2]
            print(f"[agent] net-push url: {url}")
            print(f"[agent] net-push dest: {dest}")
            local_file = download_temp_file(url)
            print(f"[agent] download_temp_file result: {local_file}")
            if local_file:
                push_cmd = f"push '{local_file}' '{dest}'"
                print(f"[agent] adb push command: {push_cmd}")
                result = run_adb_once(serial, push_cmd)
                print(f"[agent] adb push result: {result}")
                # (Tùy chọn) Xóa file sau khi push xong để tiết kiệm ổ cứng
                # try:
                #     os.remove(local_file)
                # except: pass
                return result
            else:
                print(f"[agent] Failed to download file from URL: {url}")
                return {"serial": serial, "code": 1, "stdout": "", "stderr": "Failed to download file from URL"}

    # --- XỬ LÝ LỆNH: net-install (Hỗ trợ nhiều URL + Rollback) ---
    if command_text.strip().startswith("net-install"):
        import os
        parts = shlex.split(command_text)
        urls = parts[1:]
        if not urls:
            return {"serial": serial, "code": 1, "stdout": "", "stderr": "No URLs provided", "downloaded_files": []}
        downloaded_files = []
        apk_ref_counter = {}
        installed_packages_list = []
        install_logs = []
        final_code = 0
        try:
            for i, url in enumerate(urls):
                step_num = i + 1
                local_file = download_temp_file(url)
                if not local_file:
                    install_logs.append(f"File {step_num}: Download failed ({url})")
                    final_code = 1
                    break
                # File already has .apk extension from download_temp_file() (Prevention approach)
                # No rename needed - eliminates os.rename() race condition
                downloaded_files.append(local_file)
                apk_ref_counter[local_file] = apk_ref_counter.get(local_file, 0) + 1
                packages_before = get_installed_packages(serial)
                print(f"[install] Installing {step_num}/{len(urls)}: {local_file}")
                install_cmd = f"install -r -t '{local_file}'"
                result = run_adb_once(serial, install_cmd)
                stdout = result.get("stdout", "").strip()
                stderr = result.get("stderr", "").strip()
                combined_output = f"{stdout} {stderr}"
                if "Success" in combined_output:
                    print(f"[install] File {step_num} SUCCESS.")
                    install_logs.append(f"File {step_num}: Success ({os.path.basename(url)})")
                    packages_after = get_installed_packages(serial)
                    new_packages = packages_after - packages_before
                    if new_packages:
                        pkg_name = list(new_packages)[0]
                        installed_packages_list.append(pkg_name)
                        print(f"   -> Detected new package: {pkg_name}")
                    else:
                        print("   -> No new package detected (Likely updated existing app)")
                else:
                    print(f"[install] File {step_num} FAILED. Error: {combined_output}")
                    install_logs.append(f"File {step_num}: FAILED - {combined_output}")
                    install_logs.append("!!! TRIGGERING ROLLBACK (Uninstalling previous apps) !!!")
                    final_code = 1
                    for pkg in reversed(installed_packages_list):
                        print(f"[rollback] Uninstalling {pkg}...")
                        uninstall_res = run_adb_once(serial, f"uninstall {pkg}")
                        if str(uninstall_res.get("code")) == "0":
                            install_logs.append(f"Rollback: Uninstalled {pkg} (Success)")
                        else:
                            install_logs.append(f"Rollback: Uninstalled {pkg} (Failed)")
                    break
            return {
                "serial": serial,
                "code": final_code,
                "stdout": "\n".join(install_logs),
                "stderr": "" if final_code == 0 else "Installation sequence failed with rollback.",
                "downloaded_files": downloaded_files
            }
        finally:
            # CRITICAL: Cleanup downloaded files if installation failed
            # This prevents disk space accumulation from failed net-install operations
            for file_path in downloaded_files:
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        print(f"[Cleanup] Removed failed download: {os.path.basename(file_path)}")
                except Exception as e:
                    print(f"[Cleanup] Failed to remove {os.path.basename(file_path)}: {e}")

    # --- XỬ LÝ CHUỖI LỆNH THƯỜNG ---
    steps = [step.strip() for step in command_text.split(";") if step.strip()]
    if not steps:
        return run_adb_once(serial, command_text)
    combined_stdout: List[str] = []
    combined_stderr: List[str] = []
    last_code = 0
    for step in steps:
        res = run_adb_once(serial, step)
        last_code = res.get("code", -1) or 0
        if res.get("stdout"):
            combined_stdout.append(str(res["stdout"]))
        if res.get("stderr"):
            combined_stderr.append(str(res["stderr"]))
        if last_code != 0:
            break
    return {
        "serial": serial,
        "code": last_code,
        "stdout": "\n".join(combined_stdout).strip(),
        "stderr": "\n".join(combined_stderr).strip(),
    }

# Hàm cleanup_apk_files: Xóa file APK khi không còn máy nào cần
def cleanup_apk_files(apk_files: List[str]):
    import os
    for f in apk_files:
        try:
            if os.path.exists(f):
                os.remove(f)
        except Exception:
            pass
