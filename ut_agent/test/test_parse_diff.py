"""
测试 parse_diff 工具的解析和文件写入功能。
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ut_agent.tools.parse_diff import parse_diff_files, write_parsed_files
from ut_agent.tools.save_source import write_source_files
from ut_agent.tools.fetch_dependency import _write_dependency_file


SAMPLE_DIFF_FILES = [
    {
        "filename": "src/modules/acc/acc_controller.cpp",
        "patch": (
            "@@ -15,6 +15,10 @@\n"
            " #include <algorithm>\n"
            " \n"
            "+void AccController::Enable() {\n"
            "+    enabled_ = true;\n"
            "+    count_++;\n"
            "+}\n"
            " \n"
            " void AccController::Disable() {\n"
            "@@ -45,7 +49,9 @@ void AccController::UpdateState(const SensorData& data) {\n"
            "     if (!enabled_) {\n"
            "         return;\n"
            "     }\n"
            "-    acceleration_ = 0.0;\n"
            "+    double err = data.front_distance - safe_distance_;\n"
            "+    acceleration_ = std::clamp(err * kp_, -3.0, 2.0);\n"
            "+    log_value(acceleration_);\n"
            " }"
        ),
        "edit_type": "MODIFIED",
        "language": "cpp",
    },
    {
        "filename": "src/modules/acc/acc_controller.h",
        "patch": (
            "@@ -10,6 +10,7 @@ class AccController {\n"
            " public:\n"
            "     void Disable();\n"
            "+    void Enable();\n"
            "     void UpdateState(const SensorData& data);\n"
        ),
        "edit_type": "MODIFIED",
        "language": "cpp",
    },
]


def test_parse_diff_files():
    """测试解析 diff 文件列表"""
    parsed = parse_diff_files(SAMPLE_DIFF_FILES)

    assert len(parsed) == 2

    # 第一个文件：实现文件
    cpp_file = parsed[0]
    assert cpp_file.filename == "src/modules/acc/acc_controller.cpp"
    assert cpp_file.language == "cpp"
    assert cpp_file.edit_type == "MODIFIED"
    assert len(cpp_file.hunks) == 2
    assert len(cpp_file.all_added) == 7
    assert len(cpp_file.all_deleted) == 1

    # 第二个文件：头文件
    h_file = parsed[1]
    assert h_file.filename == "src/modules/acc/acc_controller.h"
    assert len(h_file.all_added) == 1

    print("test_parse_diff_files: PASSED")


def test_write_parsed_files():
    """测试写入中间文件"""
    parsed = parse_diff_files(SAMPLE_DIFF_FILES)
    out_dir = tempfile.mkdtemp()
    mr_id = 42

    try:
        files = write_parsed_files(parsed, out_dir, mr_id)
        assert len(files) == 2

        # 检查目录结构：diff 子目录
        diff_dir = os.path.join(out_dir, f"mr_{mr_id}", "diff")
        assert os.path.isdir(diff_dir)

        # 检查文件存在
        cpp_path = os.path.join(diff_dir, "src", "modules", "acc", "acc_controller.cpp")
        assert os.path.isfile(cpp_path)

        # 检查文件内容
        with open(cpp_path, encoding="utf-8") as f:
            content = f.read()
        assert "[文件信息]" in content
        assert "filename: src/modules/acc/acc_controller.cpp" in content
        assert "[新增行]" in content
        assert "[删除行]" in content
        assert "L17:" in content

        print("test_write_parsed_files: PASSED")
    finally:
        shutil.rmtree(out_dir)


def test_write_source_files():
    """测试源文件落盘"""
    diff_files_with_head = [
        {
            "filename": "src/modules/acc/acc_controller.cpp",
            "head_file": "// full source content\nvoid Enable() { enabled_ = true; }\n",
            "edit_type": "MODIFIED",
            "language": "cpp",
        }
    ]
    out_dir = tempfile.mkdtemp()
    mr_id = 42

    try:
        files = write_source_files(diff_files_with_head, out_dir, mr_id)
        assert len(files) == 1

        src_path = os.path.join(out_dir, "mr_42", "changed_files", "src", "modules", "acc", "acc_controller.cpp")
        assert os.path.isfile(src_path)

        with open(src_path, encoding="utf-8") as f:
            content = f.read()
        assert "void Enable()" in content

        print("test_write_source_files: PASSED")
    finally:
        shutil.rmtree(out_dir)


def test_write_dependency_files():
    """测试依赖文件落盘"""
    out_dir = tempfile.mkdtemp()
    mr_id = 42

    try:
        # 写入一个成功的依赖文件
        result = _write_dependency_file(
            "class AccController { void Enable(); };",
            "src/modules/acc/acc_controller.h",
            out_dir,
            mr_id,
        )
        assert os.path.isfile(result)

        dep_path = os.path.join(out_dir, "mr_42", "deps", "src", "modules", "acc", "acc_controller.h")
        assert result == dep_path

        print("test_write_dependency_files: PASSED")
    finally:
        shutil.rmtree(out_dir)


def test_hunk_line_numbers():
    """测试 hunk 行号解析正确性"""
    parsed = parse_diff_files(SAMPLE_DIFF_FILES)
    hunk2 = parsed[0].hunks[1]

    # hunk2: @@ -45,7 +49,9 @@
    assert hunk2.old_start == 45
    assert hunk2.new_start == 49
    assert hunk2.deleted_lines[0] == (48, "    acceleration_ = 0.0;")
    assert hunk2.added_lines[0][0] == 52

    print("test_hunk_line_numbers: PASSED")


if __name__ == "__main__":
    test_parse_diff_files()
    test_write_parsed_files()
    test_write_source_files()
    test_write_dependency_files()
    test_hunk_line_numbers()
    print("\n全部测试通过 ✅")
