# Generate Patch - Python 专项规范

## 测试框架

使用 **pytest + pytest-mock** 框架。

## 文件放置

- 源文件 `src/module/foo.py` → 测试文件 `src/module/tests/test_foo.py` 或 `tests/test_foo.py`
- 遵循项目已有测试目录结构
- 测试文件命名：`test_<源文件名>.py`

## 基本结构

```python
import pytest
from module.foo import Foo, some_function


class TestFoo:
    """Foo 类的单元测试。"""

    def setup_method(self):
        """Arrange: 每个测试前准备数据。"""
        self.foo = Foo()

    def test_函数名_场景_期望结果(self):
        # Arrange
        input_data = ...
        # Act
        result = self.foo.do_something(input_data)
        # Assert
        assert result == expected

    def test_another_case(self):
        result = some_function(42)
        assert result is not None
```

## Mock 写法

```python
from unittest.mock import patch, MagicMock


class TestWithMock:
    def test_network_call_mocked(self, mocker):
        """使用 pytest-mock 的 mocker fixture。"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"key": "value"}
        
        mocker.patch("module.foo.requests.get", return_value=mock_response)
        
        result = foo.fetch_data("http://example.com")
        assert result == {"key": "value"}

    @patch("module.foo.open", create=True)
    def test_file_io_mocked(self, mock_open):
        """Mock 文件读写。"""
        mock_open.return_value.__enter__.return_value.read.return_value = "content"
        result = foo.read_config("config.yaml")
        assert result == "content"
```

## 参数化测试

```python
import pytest


@pytest.mark.parametrize("a, b, expected", [
    (1, 2, 3),
    (0, 0, 0),
    (-1, 1, 0),
    (999999, 1, 1000000),
])
def test_add_various_inputs(a, b, expected):
    assert add(a, b) == expected
```

## 异常测试

```python
def test_invalid_input_raises():
    with pytest.raises(ValueError, match="cannot be negative"):
        process(-1)

def test_none_input_raises():
    with pytest.raises(TypeError):
        process(None)
```

## 输出捕获（覆盖 print/logging 变更行）

```python
def test_print_output(capsys):
    foo.print_info()
    captured = capsys.readouterr()
    assert "expected content" in captured.out

def test_logging_output(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        foo.do_risky_thing()
    assert "warning message" in caplog.text
```

## Fixture 用法

```python
@pytest.fixture
def db_connection(mocker):
    """Mock 数据库连接。"""
    mock_conn = MagicMock()
    mock_conn.execute.return_value = [{"id": 1, "name": "test"}]
    mocker.patch("module.foo.get_connection", return_value=mock_conn)
    return mock_conn

def test_query_users(db_connection):
    result = query_users()
    assert len(result) == 1
    assert result[0]["name"] == "test"
```

## 注意事项

- import 路径必须与项目实际包结构一致
- Python 的 `_private` 方法虽然可以访问，但仍应通过公共接口测试
- 使用 `mocker.patch` 时，patch 的路径是**被测模块中的引用路径**，不是定义路径
  - 例如：`foo.py` 中 `from requests import get`，patch 路径是 `module.foo.get`
- 异步函数用 `pytest-asyncio`：`@pytest.mark.asyncio` + `async def test_...`
- ROS2 Python 节点：Mock 掉 `rclpy.init()`、publisher、subscriber、service client
