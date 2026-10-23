"""Source launcher: use an existing Python runtime with -I -B -S."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parent))
from projectrecall.cli import main
if __name__=='__main__':
    raise SystemExit(main())