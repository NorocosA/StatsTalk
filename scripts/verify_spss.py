#!/usr/bin/env python
"""
P0 验证脚本：SPSS 批处理连通性测试

用法:
    python scripts/verify_spss.py --spss-path "C:\\...\\stats.exe" --data-file "test_data.sav" --output-dir ./p0_output
"""

import argparse
import json
import subprocess
import sys
import os
import tempfile
from datetime import datetime


# ========== 测试用 SPSS 语法 ==========
TEST_SYNTAX = [
    {"name": "FREQUENCIES", "syntax": "FREQUENCIES VARIABLES=gender.\n"},
    {"name": "DESCRIPTIVES", "syntax": "DESCRIPTIVES VARIABLES=score.\n"},
    {"name": "T-TEST", "syntax": "T-TEST GROUPS=gender(1 2) /VARIABLES=score.\n"},
    {"name": "CROSSTABS", "syntax": "CROSSTABS TABLES=gender BY class.\n"},
    {"name": "REGRESSION", "syntax": "REGRESSION /DEPENDENT=score /METHOD=ENTER age.\n"},
]

# 每个语法前自动加载数据集
GET_FILE_PREAMBLE = "GET FILE='{data_path}'.\nDATASET NAME P0Test.\n"


def write_syntax_file(syntax: str, output_dir: str, name: str, data_path: str = None) -> str:
    """将语法写入 .sps 文件，返回文件路径"""
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"{name}.sps")
    with open(path, "w", encoding="utf-8") as f:
        if data_path:
            f.write(GET_FILE_PREAMBLE.format(data_path=data_path))
        f.write(syntax)
    return path


def run_syntax(spss_path: str, sps_path: str, output_dir: str, name: str) -> dict:
    """调用 spss.exe 执行语法，返回结果字典

    SPSS 26 batch mode flags:
    - production silent: suppress all dialogs/prompts
    - background: no splash screen
    """
    log_path = os.path.join(output_dir, f"run_{name}.log")

    try:
        result = subprocess.run(
            [spss_path, "-production", "silent", "-background", "-nologo", sps_path],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=output_dir,
        )
    except subprocess.TimeoutExpired:
        return {
            "name": name,
            "exit_code": -1,
            "stdout": "",
            "stderr": "TIMEOUT: SPSS process exceeded 120s",
            "success": False,
        }
    except FileNotFoundError:
        return {
            "name": name,
            "exit_code": -2,
            "stdout": "",
            "stderr": f"SPSS executable not found: {spss_path}",
            "success": False,
        }

    # 写入日志文件
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"EXIT CODE: {result.returncode}\n")
        f.write(f"STDOUT:\n{result.stdout}\n")
        f.write(f"STDERR:\n{result.stderr}\n")

    return {
        "name": name,
        "exit_code": result.returncode,
        "stdout": result.stdout[:500],
        "stderr": result.stderr[:500],
        "success": result.returncode == 0,
        "log_file": log_path,
    }


def main():
    parser = argparse.ArgumentParser(description="SPSS batch connectivity test")
    parser.add_argument("--spss-path", required=True, help="Path to spss.exe / stats.exe")
    parser.add_argument(
        "--output-dir", default="./p0_output", help="Output directory for logs and XML"
    )
    parser.add_argument("--data-file", required=True, help="Path to .sav test dataset")
    args = parser.parse_args()

    spss_path = args.spss_path
    output_dir = args.output_dir
    data_file = os.path.abspath(args.data_file)
    os.makedirs(output_dir, exist_ok=True)

    if not os.path.exists(data_file):
        print(f"ERROR: Data file not found: {data_file}")
        sys.exit(1)

    print(f"SPSS Path: {spss_path}")
    print(f"Data File: {data_file}")
    print(f"Output Dir: {output_dir}")
    print(f"Time: {datetime.now().isoformat()}")
    print("-" * 60)

    results = []
    for test in TEST_SYNTAX:
        name = test["name"]
        print(f"Running: {name} ...", end=" ", flush=True)

        sps_path = write_syntax_file(test["syntax"], output_dir, name, data_path=data_file)
        result = run_syntax(spss_path, sps_path, output_dir, name)
        results.append(result)

        status = "OK" if result["success"] else f"FAIL (code={result['exit_code']})"
        print(status)

    # 输出汇总报告
    passed = sum(1 for r in results if r["success"])
    total = len(results)
    print("-" * 60)
    print(f"Result: {passed}/{total} passed")

    report = {
        "timestamp": datetime.now().isoformat(),
        "spss_path": spss_path,
        "passed": passed,
        "total": total,
        "results": results,
    }
    report_path = os.path.join(output_dir, "connectivity_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"Report saved: {report_path}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
