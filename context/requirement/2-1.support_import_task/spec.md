# 支持导入xml格式的任务

## 格式定义模板：
根据下面格式定义的模板，结合代码的实现，定义一个xsd。xsd相关注释写清楚，能指导大模型去生成对应的xml
```xml
<task name='xxxx'>
<parameters>  # 下面有多个param，定义全局的param
    <parameter type='fixed'>  # 定义全局param
        <name>p_1</name>
        <value>v_1</name>
    </parameter>
        <parameter type='fixed'>  # 定义全局param
        <name>p_2</name>
        <value>v_2</name>
    </parameter>
</parameters>

<taskStep order=1> # order是执行顺序
    <rest method='GET' path ='XXXXXXXXX'> # 1个taskStep只能有1个rest
        <header>
            <name></name>
            <value></value>
        </header>
        <header>
            <name></name>
            <value></value>
        </header>
        <body>  #放的请求参数
            <kv>
                <key>p1</key>
                <value> v1 </value>
            </kv>
            <kv>
                <paremeter>x1</parameter> # 引用parameter的name
            </kv>
        </body>
        <assertionCode> </assertionCode> 断言代码，python格式，没有标签默认为空
        <extratParam>  # 从rest的结果里抽取全局param
          <name>p2</name>
          <value>data[0].param</value> #responseBody的表达式
        </extractParam>
    </rest>
</taskStep>

<taskStep order=2> # order是执行顺序，必要的
    <rest method='GET' path ='XXXXXXXXX'> # 1个taskStep只能有1个rest
        <header>
            <name></name>
            <value></value>
        </header>
        <header>
            <name></name>
            <value></value>
        </header>
        <body>  #放的请求参数
            <kv>
                <key>p1</key>
                <value> v1 </value>
            </kv>
            <kv>
                <paremeter>x1</parameter> # 引用parameter的name
            </kv>
        </body>
        <assertionCode> </assertionCode> 断言代码
        <extratParam>  # 从rest的结果里抽取全局param
          <name>p2</name>
          <value>data[0].param</value> #responseBody的表达式
        </extractParam>
    </rest>
</taskStep>


</task>
```

