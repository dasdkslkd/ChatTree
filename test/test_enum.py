from typing import TypedDict, List, Optional, Dict, Any, Union, Required
from enum import Enum
import json

class EnumABC(str, Enum):
    A = 'a'
    B = 'b'

x = {EnumABC.A: 'aaa', EnumABC.B: 'bbb'}
with open('test.json','w') as f:
    json.dump(x,f,indent=2)