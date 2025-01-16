# 一、介绍
我是Mr.看海，我在尝试用信号处理的知识积累和思考方式做量化交易，应用深度学习和AI实现股票自动交易，目的是实现财务自由~

目前我正在开发基于miniQMT的量化交易系统——看海量化交易系统。

我的微信公众号是：看海的城堡

我的知乎账户是：Mr.看海

# 二、我为什么要自己搭建一套量化交易框架？
在诸多现有量化交易系统可用的当下，我为什么要花这么大的力气再自己做一遍这件事。

1.想要足够开放灵活地实现量化交易。现有的量化交易系统往往对策略开发的方式有较多限制，比如对支持的python第三方包不开放，就限制了最先进的AI算法在量化交易中的应用，这对于我未来想在量化交易领域开展的探索是致命性的约束。基于miniQMT的系统开发几乎将所有自由度都交给了开发者，这是极具诱惑力的。

2.想要策略安全地本地运行。此外基于miniQMT的系统，所有的策略都在本地运行，我们只是调用了行情和交易接口，对于量化交易来说，策略就是一切，本地运行无疑是最安全的。其次是在需要进行大规模数据处理或复杂模型训练时，本地化部署能够提供更好的性能和更低的成本。

3.想要打造一个趁手的“兵器”。关注我的老粉丝可能知道，我以前主要是做信号处理和机器学习算法研究的，在前几年的开发中就有着将工具高效化的开发习惯，在保证专业性的同时实现高度的易用性，软件毕竟只是工具，未来大部分经历是要放在策略研究上的，所以我对这个自己要用的工具的要求就是要足够好用。

最重要的，目前还没有基于miniQMT回测和模拟平台，我开发khQuant框架来吃这个螃蟹，也是给自己和读者朋友们提供更多的可选择空间。

关于该系统的更多介绍：
https://khsci.com/khQuant/

# 三、目前已经完成的功能
截至2025年1月16日，已经完成数据下载、数据补充、数据清洗和数据可视化功能。具体进展可以看开发日志（最新的开发进展请关注微信公众号“看海的城堡”）：

[【深度学习量化交易1】一个金融小白尝试量化交易的设想、畅享和遐想](https://mp.weixin.qq.com/s?__biz=MzUzNDk1NjcyNg==&mid=2247484609&idx=1&sn=7ec0b44a90e3a213332fa7e53ed514a8&scene=21#wechat_redirect)

[【深度学习量化交易2】财务自由第一步，三个多月的尝试，找到了最合适我的量化交易路径](https://mp.weixin.qq.com/s?__biz=MzUzNDk1NjcyNg==&mid=2247484748&idx=1&sn=df5365c8d7ba1890ccb69984f9063b07&scene=21#wechat_redirect)

[【深度学习量化交易3】为了轻松免费地下载股票历史数据，我开发完成了可视化的数据下载模块](https://mp.weixin.qq.com/s?__biz=MzUzNDk1NjcyNg==&mid=2247484773&idx=1&sn=2df74f66523a7e3be4d1f60bd5f03322&scene=21#wechat_redirect)

[【深度学习量化交易4】 量化交易历史数据清洗——为后续分析扫清障碍](https://mp.weixin.qq.com/s?__biz=MzUzNDk1NjcyNg==&mid=2247484790&idx=1&sn=8e9f08d57a4f4fc298b153cee699b09a&scene=21#wechat_redirect)

[【深度学习量化交易5】 量化交易历史数据可视化模块](https://mp.weixin.qq.com/s?__biz=MzUzNDk1NjcyNg==&mid=2247484808&idx=1&sn=b22fbc5b0349324832d10a5cb97fbfd6&scene=21#wechat_redirect)

[【深度学习量化交易6】优化改造基于miniQMT的量化交易软件，已开放下载\~（已完成数据下载、数据清洗、可视化模块）](https://mp.weixin.qq.com/s?__biz=MzUzNDk1NjcyNg==&mid=2247484854&idx=1&sn=38c07a49e9826b2f82bb23baacb709bf&scene=21#wechat_redirect)

[【深度学习量化交易7】miniQMT快速上手教程案例集——使用xtQuant进行历史数据下载篇](https://mp.weixin.qq.com/s?__biz=MzUzNDk1NjcyNg==&mid=2247484875&idx=1&sn=53942f6ee87b43e8a8a362243421d340&scene=21#wechat_redirect)

[【深度学习量化交易8】miniQMT快速上手教程案例集——使用xtQuant进行获取实时行情数据篇](https://mp.weixin.qq.com/s?__biz=MzUzNDk1NjcyNg==&mid=2247484893&idx=1&sn=072481343fdcfbc478a95af42fdbc7f1&scene=21#wechat_redirect)

[【深度学习量化交易9】miniQMT快速上手教程案例集——使用xtQuant获取基本面数据篇](https://mp.weixin.qq.com/s?__biz=MzUzNDk1NjcyNg==&mid=2247484894&idx=1&sn=be58c5985965b4d960fcc0982c83a1a2&scene=21#wechat_redirect)

[【深度学习量化交易10】miniQMT快速上手教程案例集——使用xtQuant获取板块及成分股数据篇](https://mp.weixin.qq.com/s?__biz=MzUzNDk1NjcyNg==&mid=2247484904&idx=1&sn=b91f208db8b3ad84ddc7d03822e3b80b&scene=21#wechat_redirect)

[【深度学习量化交易11】miniQMT快速上手教程——使用XtQuant进行实盘交易篇（八千字超详细版本）](https://mp.weixin.qq.com/s?__biz=MzUzNDk1NjcyNg==&mid=2247484912&idx=1&sn=611350909bb70f422699dbcb55348479&scene=21#wechat_redirect)

[【深度学习量化交易12】基于miniQMT的量化交易框架总体构建思路——回测、模拟、实盘通吃的系统架构](https://mp.weixin.qq.com/s?__biz=MzUzNDk1NjcyNg==&mid=2247484932&idx=1&sn=2fad21355d5840603c1b76176cc452e3&scene=21#wechat_redirect)

# 四、开通MiniQMT

上述系统是基于miniQMT，很多券商都可以开通miniQMT，不过门槛各有不同，很多朋友找不到合适的券商和开通渠道。这里我可以联系券商渠道帮忙开通，股票交易费率是万1。这个系统还在持续开发的过程中，使用这套软件的朋友们也欢迎大家多提提意见，我也会及时响应，完善功能。

对于想要开通miniQMT、使用上边开发的“看海量化交易系统”的朋友们，请大家关注一下我的公众号“看海的城堡”，在公众号页面下方点击相应标签即可获取。也可以直接点击这里：如何轻松开通miniQMT，开启量化交易大门

# 四、如何使用
上述开源代码中的主程序为GUI.py

运行该代码需要自行安装相关库。

此外我还还提供了打包好的exe文件，直接安装即可，对于没有开发环境的朋友们推荐直接使用exe文件（exe文件就是用本开源代码打包封装的）

exe文件的下载地址为：https://khsci.com/khQuant/

# 注意！本开源代码采用CC BY-NC 4.0许可协议，禁止任何商用用途！

CC BY-NC 4.0许可

你可以自由地：

分享 - 复制并以任何格式重新分发材料

改编 - 混合、转换并从材料中构建

只要你遵守许可条款，许可方不能撤销这些自由。

在以下条款下：

署名 - 你必须适当注明出处，提供许可链接，并说明是否进行了任何更改。你可以以任何合理的方式这样做，但不得以暗示你或你的使用得到许可方认可的方式。

非商业性 - 你不得将材料用于商业目的。

没有附加限制 - 你不得适用法律术语或技术措施，在法律上限制他人进行许可允许的任何使用。
