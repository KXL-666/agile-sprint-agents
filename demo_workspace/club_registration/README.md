# 社团报名演示项目

这是 AgileSprint Agents 的答辩演示输入项目。

当前 `registration.py` 故意没有实现“重复学号不得报名”的规则，因此执行：

```powershell
python -m pytest -q
```

会出现 1 个失败测试。你可以把本文件夹接入 AgileSprint Agents，并输入需求：

> 为社团报名模块增加重复学号校验。重复报名时抛出“学号已报名”错误，并保证现有正常报名功能不受影响。

真实模型会读取源码、提交 `registration.py` 的修改提案；批准后运行测试，即可展示测试失败、修复提案与复测通过。
