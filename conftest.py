# 根 conftest：测试进程中隔离 CHATTREE_HOME 到临时目录，
# 避免无参 RunManager()/RunJournal() 等把测试残留写入用户真实 ~/.chattree。
import os
import tempfile

_TEST_HOME = tempfile.mkdtemp(prefix="chattree-test-home-")
os.environ["CHATTREE_HOME"] = _TEST_HOME