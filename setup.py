#!/usr/bin/env python3
"""
Setup script for mdsync
"""

from setuptools import setup, find_packages
from pathlib import Path

# Read the README file
this_directory = Path(__file__).parent
long_description = (this_directory / "README.md").read_text()

setup(
    name='mdsync',
    version='0.2.3',
    description='Sync between Google Docs and Markdown files',
    long_description=long_description,
    long_description_content_type='text/markdown',
    author='Chase Pettet',
    author_email='',
    url='https://github.com/chasemp/mdsync',
    license='MIT',
    py_modules=['mdsync'],
    python_requires='>=3.7',
    install_requires=[
        'google-auth>=2.23.0',
        'google-auth-oauthlib>=1.1.0',
        'google-auth-httplib2>=0.1.1',
        'google-api-python-client>=2.100.0',
        'markdown>=3.5.0',
        'atlassian-python-api>=3.41.0',
        'requests>=2.31.0',
        'pyyaml>=6.0',
        'beautifulsoup4>=4.12.0',
        'html2text>=2020.1.16',
        'python-frontmatter>=1.0.0',
    ],
    entry_points={
        'console_scripts': [
            'mdsync=mdsync:main',
        ],
    },
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Topic :: Utilities',
        'Topic :: Text Processing :: Markup :: Markdown',
    ],
    keywords='google-docs markdown sync converter',
)
