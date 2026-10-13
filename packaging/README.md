# 打包版冒烟测试

在项目根目录安装 `requirements.lock` 和 `requirements-build.lock` 后，用当前 `khQuant_inno.spec` 构建，再运行：

```powershell
./packaging/smoke_test.ps1
# 指定安装目录中的 exe 和构建环境的 Python：
./packaging/smoke_test.ps1 -Exe 'C:\Program Files\khQuantOS\khQuantOS.exe' -Python '.venv\Scripts\python.exe' -Seconds 60
```

测试为每次运行创建独立目录、单实例标识和 QSettings INI。被测程序通过 QSettings 读取的是隔离目录，不会导入真实旧版界面设置或把测试设置写进用户注册表。进程在恢复执行前加入 Windows 作业对象，测试成功、超时或父进程提前退出时均清理本次进程树；其他已打开的程序保持运行。

终端输出 `report.json` 路径。报告记录启动结果、清理后的活动进程数，以及真实界面注册表与 OS 配置文件的前后指纹是否一致；不输出其中的设置值。失败返回非零退出码，日志和隔离目录保留供排查。

旧包缺少 `smoke_runtime` 运行钩子时会在启动前拒绝测试，应使用更新后的 spec 重新构建。正常启动未设置 `KHQUANT_SMOKE_ROOT` 时，运行钩子不加载 Qt、不改变环境或设置。`KHQUANT_SMOKE_ROOT` 与对应运行标识只由测试控制器临时传给子进程，不应加入用户日常环境变量。

这项检查验证打包启动和隔离清理。发布安装包还应在独立 Windows 沙盒中验收首次引导、下载、回测、重启、旧版共存及卸载。
