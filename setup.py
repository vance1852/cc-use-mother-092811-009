from setuptools import find_packages, setup

setup(
    name="transport-coordination",
    version="0.2.0",
    description="综合交通协同服务：普通公路养护资金决策与执行",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.11",
)
