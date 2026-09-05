# Generate Patch - C++ 专项规范

## 测试框架

使用 **GTest + GMock** 框架。

## 文件放置

- 源文件 `src/module/foo.cpp` → 测试文件 `src/module/test/test_foo.cpp`
- 遵循项目已有 `test/` 或 `tests/` 目录结构
- 测试文件命名：`test_<源文件名>.cpp`

## 基本结构

```cpp
#include <gtest/gtest.h>
#include <gmock/gmock.h>
#include "被测头文件.h"

class FooTest : public ::testing::Test {
protected:
    void SetUp() override {
        // Arrange: 准备测试数据
    }
    void TearDown() override {
        // 清理
    }
};

TEST_F(FooTest, test_函数名_场景_期望结果) {
    // Arrange
    // Act
    // Assert
    EXPECT_EQ(actual, expected);
}
```

## Mock 写法

```cpp
#include <gmock/gmock.h>

class MockDependency : public IDependency {
public:
    MOCK_METHOD(ReturnType, MethodName, (ParamType1, ParamType2), (override));
};

TEST_F(FooTest, test_with_mock) {
    MockDependency mock_dep;
    EXPECT_CALL(mock_dep, MethodName(testing::_, testing::_))
        .WillOnce(testing::Return(expected_value));
    
    Foo foo(&mock_dep);
    auto result = foo.DoSomething();
    EXPECT_EQ(result, expected_value);
}
```

## 参数化测试

```cpp
class FooParamTest : public ::testing::TestWithParam<std::tuple<int, int, int>> {};

TEST_P(FooParamTest, test_add_various_inputs) {
    auto [a, b, expected] = GetParam();
    EXPECT_EQ(add(a, b), expected);
}

INSTANTIATE_TEST_SUITE_P(AddTests, FooParamTest,
    ::testing::Values(
        std::make_tuple(1, 2, 3),
        std::make_tuple(0, 0, 0),
        std::make_tuple(-1, 1, 0)
    )
);
```

## 异常测试

```cpp
TEST_F(FooTest, test_invalid_input_throws) {
    EXPECT_THROW(foo.Process(nullptr), std::invalid_argument);
    EXPECT_NO_THROW(foo.Process(valid_ptr));
}
```

## 输出捕获（覆盖 cout/cerr 变更行）

```cpp
TEST_F(FooTest, test_print_output) {
    testing::internal::CaptureStdout();
    foo.PrintInfo();
    std::string output = testing::internal::GetCapturedStdout();
    EXPECT_THAT(output, testing::HasSubstr("expected content"));
}
```

## 注意事项

- `#include` 路径必须相对于仓库根目录或 CMake include 目录
- 如果被测类有虚函数接口，优先通过接口 Mock
- 如果没有虚函数接口（紧耦合），通过构造函数注入或模板参数注入 Mock
- 不要使用 `#define private public` hack
- ROS2 节点测试：使用 `rclcpp::init()` / `rclcpp::shutdown()` 在 fixture 中管理生命周期，Mock 掉 service/publisher/subscriber
