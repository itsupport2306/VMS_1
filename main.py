#!/usr/bin/env python3
"""
Vendor Management System - Main Entry Point
"""

import os
import sys
import subprocess
import argparse
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))
#fucntion to run backend server and specifies 
def run_backend():
    """Start the FastAPI backend server"""
    print("🚀 Starting VMS Backend Server...")
    print("📍 Backend will be available at: http://localhost:8000")
    print("📚 API Docs: http://localhost:8000/docs")
    print("⏹️  Press Ctrl+C to stop the server")
    print("-" * 50)
    
    try:
        import uvicorn
        from backend.main import app
        
        uvicorn.run(
            "backend.main:app",
            host="0.0.0.0",
            port=8000,
            reload=True,  # Auto-reload on code changes
            log_level="info"
        )
    except ImportError as e:
        print(f"❌ Missing dependencies: {e}")
        print("💡 Please install requirements: pip install -r requirements.txt")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Failed to start backend: {e}")
        sys.exit(1)

def run_frontend():
    """Start the Streamlit frontend"""
    print("🎨 Starting VMS Frontend Dashboard...")
    print("📍 Frontend will be available at: http://localhost:8501")
    print("⏹️  Press Ctrl+C to stop the server")
    print("-" * 50)
    
    try:
        import streamlit.web.cli as stcli
        
        # Change to frontend directory
        frontend_path = project_root / "frontend"
        sys.argv = ["streamlit", "run", "main.py", "--server.port", "8501"]
        
        os.chdir(frontend_path)
        stcli.main()
        
    except ImportError as e:
        print(f"❌ Missing dependencies: {e}")
        print("💡 Please install requirements: pip install -r requirements.txt")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Failed to start frontend: {e}")
        sys.exit(1)

def setup_database():
    """Create database tables"""
    print("🗄️  Setting up database tables...")
    
    try:
        from database.models import create_tables
        create_tables()
        print("✅ Database tables created successfully!")
    except ImportError as e:
        print(f"❌ Missing dependencies: {e}")
        print("💡 Please install requirements: pip install -r requirements.txt")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Failed to create database tables: {e}")
        print("💡 Please check your DATABASE_URL in .env file")
        sys.exit(1)

def install_requirements():
    """Install required packages"""
    print("📦 Installing requirements...")
    
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
        print("✅ Requirements installed successfully!")
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to install requirements: {e}")
        sys.exit(1)

def create_env_file():
    """Create .env file from example"""
    env_example = project_root / ".env.example"
    env_file = project_root / ".env"
    
    if env_file.exists():
        print("⚠️  .env file already exists!")
        return
    
    try:
        with open(env_example, 'r') as f:
            content = f.read()
        
        with open(env_file, 'w') as f:
            f.write(content)
        
        print("✅ .env file created from example!")
        print("💡 Please edit .env file with your configuration")
    except FileNotFoundError:
        print("❌ .env.example file not found!")
    except Exception as e:
        print(f"❌ Failed to create .env file: {e}")

def show_status():
    """Show system status"""
    print("📊 VMS System Status")
    print("=" * 30)
    
    # Check if .env exists
    env_file = project_root / ".env"
    if env_file.exists():
        print("✅ Configuration file (.env) exists")
    else:
        print("❌ Configuration file (.env) missing")
        print("💡 Run: python main.py setup")
    
    # Check if uploads directory exists
    uploads_dir = project_root / "uploads"
    if uploads_dir.exists():
        print("✅ Uploads directory exists")
    else:
        print("❌ Uploads directory missing")
        uploads_dir.mkdir(exist_ok=True)
        print("✅ Created uploads directory")
    
    # Check dependencies
    try:
        import fastapi
        import streamlit
        import sqlalchemy
        print("✅ Core dependencies installed")
    except ImportError as e:
        print(f"❌ Missing dependencies: {e}")
        print("💡 Run: python main.py install")
    
    print("-" * 30)

def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Vendor Management System")
    parser.add_argument("command", choices=[
        "backend", "frontend", "setup", "install", "status", "all"
    ], help="Command to run")
    
    args = parser.parse_args()
    
    print("🏢 Vendor Management System")
    print("=" * 30)
    
    if args.command == "backend":
        run_backend()
    elif args.command == "frontend":
        run_frontend()
    elif args.command == "setup":
        create_env_file()
        setup_database()
        print("\n🎉 Setup completed!")
        print("💡 Next steps:")
        print("   1. Edit .env file with your configuration")
        print("   2. Run: python main.py backend")
        print("   3. Run: python main.py frontend")
    elif args.command == "install":
        install_requirements()
    elif args.command == "status":
        show_status()
    elif args.command == "all":
        print("🚀 Starting complete VMS system...")
        print("This will start both backend and frontend")
        print("Backend: http://localhost:8000")
        print("Frontend: http://localhost:8501")
        print("-" * 30)
        
        # Start backend in background
        import threading
        backend_thread = threading.Thread(target=run_backend, daemon=True)
        backend_thread.start()
        
        # Give backend time to start
        import time
        time.sleep(3)
        
        # Start frontend
        run_frontend()

if __name__ == "__main__":
    main()
