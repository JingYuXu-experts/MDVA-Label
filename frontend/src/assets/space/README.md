# 三维视景纹理与来源

这些文件是用于三维表面渲染的静态材质，不代表当前太阳、云层或夜间灯光观测，也不作为识别模型输入。

| 文件 | 来源 |
|---|---|
| earth-day.jpg | Three.js examples / textures/planets/earth_atmos_2048.jpg |
| earth-specular.jpg | Three.js examples / textures/planets/earth_specular_2048.jpg |
| earth-night.jpg | Solar System Scope / 2k_earth_nightmap.jpg |
| earth-clouds.jpg | Solar System Scope / 2k_earth_clouds.jpg |
| sun.jpg | Solar System Scope / 2k_sun.jpg |

Solar System Scope / INOVE： https://www.solarsystemscope.com/textures/ ，纹理按 CC BY 4.0 发布；本项目将其用于着色、叠加与大气表现，未将其标为实时观测。对应归属也显示在三维视图的“数据依据”中。

Three.js 示例资源来源：https://github.com/mrdoob/three.js/tree/dev/examples/textures/planets ，Three.js 为 MIT 项目。下载 URL 与字节数记录见 sources.json。

太阳着色、日冕、传播薄壳与粒子层是本平台编写的渲染表达。CME 颜色与体积纹理不编码实测密度；恒速外推不等于磁流体求解或地球到达预测。
