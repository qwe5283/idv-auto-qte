# idv-auto-qte

自动处理第五人格中，修机、治疗时出现的校准事件（QTE快速反应事件）

## 如何使用

克隆代码库

```
git clone https://github.com/qwe5283/idv-auto-qte.git && cd idv-auto-qte
```

安装依赖

```
pip install -r requirements.txt
```

启动截图服务（需要以管理员权限运行）

```
python app.py
```

启动后就可以切到第五人格PC客户端或MuMu模拟器(推荐)，程序会开始截图并托管校准事件

---

你也可以传入自己的录屏文件查看识别效果

```
python app.py screenrecord.mp4
```