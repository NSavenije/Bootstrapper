from setuptools import setup, find_packages

setup(
    name='bootstrapper',
    version='0.1.0',
    packages=find_packages(),
    install_requires=[
        'click',
        'requests',
        'pyyaml',
        'hcloud',
        'paramiko',
        'jinja2',
    ],
    entry_points={
        'console_scripts': [
            'bootstrapper=bootstrapper.cli:cli',
        ],
    },
    author='Noud Savenije',
    description='CLI tool to provision infrastructure and deploy Forgejo & Authentik',
    include_package_data=True,
    package_data={
        'bootstrapper': ['templates/*.j2'],
    },
    zip_safe=False,
)
