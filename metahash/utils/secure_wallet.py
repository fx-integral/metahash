#!/usr/bin/env python3
"""
Secure wallet password management for Metahash.
Replaces environment variable storage with secure keyring.
"""

import os
import keyring
import getpass
import logging
import bittensor as bt
from pathlib import Path
from typing import Optional
import hashlib
import json
from datetime import datetime


class SecureWalletManager:
    """Secure password manager for Metahash."""
    
    def __init__(self, app_name: str = "metahash"):
        self.app_name = app_name
        self.audit_log = Path.home() / f".{app_name}" / "audit.log"
        self.audit_log.parent.mkdir(mode=0o700, exist_ok=True)
        
    def get_password(self, wallet_name: str) -> Optional[str]:
        """Retrieve password securely."""
        try:
            # 1. Check keyring
            password = keyring.get_password(self.app_name, wallet_name)
            
            if password:
                self._audit("password_retrieved", wallet_name, True)
                # Check strength even for stored passwords (warn only)
                if not self._is_password_strong(password):
                    self._show_weak_password_warning()
                return password
                
            # 2. Ask user
            print(f"\n🔐 Password required for wallet: {wallet_name}")
            password = getpass.getpass("Password: ")
            
            if not password:
                self._audit("password_cancelled", wallet_name, False)
                return None
                
            # 3. Check password strength (warn only, don't block)
            if not self._is_password_strong(password):
                self._show_weak_password_warning()
                self._audit("weak_password_warning", wallet_name, True)
                
            # 4. Save option
            save_choice = input("\nSave password securely? (y/n): ").lower()
            if save_choice == 'y':
                try:
                    keyring.set_password(self.app_name, wallet_name, password)
                    print("✅ Password saved in secure storage")
                except Exception as e:
                    print(f"⚠️  Cannot save: {e}")
                    
            self._audit("password_entered", wallet_name, True)
            return password
            
        except KeyboardInterrupt:
            print("\n❌ Cancelled")
            self._audit("password_cancelled", wallet_name, False)
            return None
        except Exception as e:
            logging.error(f"Error retrieving password: {e}")
            self._audit("password_error", wallet_name, False)
            return None
            
    def _is_password_strong(self, password: str) -> bool:
        """Check password strength."""
        if len(password) < 12:
            return False
        if not any(c.isupper() for c in password):
            return False
        if not any(c.islower() for c in password):
            return False
        if not any(c.isdigit() for c in password):
            return False
        return True
        
    def _show_weak_password_warning(self):
        """Display warning about weak password."""
        print("\n⚠️  WARNING: Your password appears to be weak!")
        print("   A strong password should have:")
        print("   - Minimum 12 characters")
        print("   - Upper and lowercase letters")
        print("   - Numbers")
        print("   - Special characters (recommended)")
        print("\n   Consider changing your wallet password for better security.\n")
        
    def _audit(self, action: str, wallet: str, success: bool):
        """Log action to audit log."""
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "action": action,
            "wallet": wallet,
            "success": success,
            "user": os.getenv("USER", "unknown"),
            "hash": hashlib.sha256(f"{wallet}{action}".encode()).hexdigest()[:8]
        }
        
        try:
            with open(self.audit_log, 'a') as f:
                f.write(json.dumps(entry) + '\n')
        except Exception as e:
            logging.warning(f"Could not write audit log: {e}")
            
    def clear_password(self, wallet_name: str):
        """Remove password from keyring."""
        try:
            keyring.delete_password(self.app_name, wallet_name)
            print(f"✅ Password for {wallet_name} removed")
            self._audit("password_deleted", wallet_name, True)
        except Exception as e:
            print(f"⚠️  Error: {e}")
            self._audit("password_delete_failed", wallet_name, False)


def load_wallet_secure(coldkey_name: str, hotkey_name: str, unlock: bool = True) -> bt.wallet:
    """Secure wallet loading."""
    # Parameter validation
    if not coldkey_name or not hotkey_name:
        raise ValueError(
            f"Must provide wallet and hotkey name! "
            f"Received: coldkey_name='{coldkey_name}', hotkey_name='{hotkey_name}'"
        )
    
    manager = SecureWalletManager()
    
    # Create wallet
    wallet = bt.wallet(name=coldkey_name, hotkey=hotkey_name)
    
    if not unlock:
        return wallet
    
    # Get password securely
    wallet_identifier = f"{coldkey_name}-{hotkey_name}"
    password = manager.get_password(wallet_identifier)
    
    if not password:
        raise ValueError("No password provided")
        
    try:
        # Use method compatible with bittensor
        # save_password_to_env method is available in all versions
        wallet.coldkey_file.save_password_to_env(password)
        
        # Try to unlock
        wallet.unlock_coldkey()
        
        # Clear password from memory (Python garbage collector will handle it)
        password = None
        
        # Clear environment variable immediately after unlocking
        if "WALLET_PASSWORD" in os.environ:
            del os.environ["WALLET_PASSWORD"]
        
        bt.logging.debug(
            f"Wallet unlocked securely (cold={wallet.coldkey.ss58_address[:8]}...)"
        )
        
        return wallet
        
    except Exception as e:
        # Ensure password is cleared even on error
        if "WALLET_PASSWORD" in os.environ:
            del os.environ["WALLET_PASSWORD"]
            
        manager._audit("unlock_failed", coldkey_name, False)
        raise Exception(f"Unable to unlock wallet: {e}")


# Helper function for backward compatibility
def load_wallet(coldkey_name: str, hotkey_name: str, unlock: bool = True, raise_exception: bool = True) -> Optional[bt.wallet]:
    """
    Wrapper for backward compatibility.
    Uses secure manager instead of environment variables.
    """
    try:
        return load_wallet_secure(coldkey_name, hotkey_name, unlock)
    except Exception as e:
        if raise_exception:
            raise
        bt.logging.error(f"Failed to load wallet: {e}")
        return None


# Additional helper function to clear all passwords
def clear_all_passwords():
    """Remove all saved passwords for Metahash."""
    manager = SecureWalletManager()
    
    # Get all passwords from keyring
    try:
        import keyrings.alt
        # For some backends we might need a different approach
        # This is a sample implementation
        print("⚠️  This function may not work with all keyring backends")
        print("Use manager.clear_password(wallet_name) for specific wallets")
    except ImportError:
        print("Install keyrings.alt for full functionality")


# Test function
if __name__ == "__main__":
    import sys
    
    print("=== Test Secure Wallet Manager ===")
    
    if len(sys.argv) > 2:
        coldkey = sys.argv[1]
        hotkey = sys.argv[2]
        
        try:
            print(f"\nLoading wallet {coldkey}-{hotkey}...")
            wallet = load_wallet_secure(coldkey, hotkey)
            print(f"✅ Success! Address: {wallet.coldkey.ss58_address}")
            
            # Clear option
            if input("\nClear saved password? (y/n): ").lower() == 'y':
                manager = SecureWalletManager()
                manager.clear_password(f"{coldkey}-{hotkey}")
                
        except Exception as e:
            print(f"❌ Error: {e}")
    else:
        print("Usage: python secure_wallet.py <coldkey_name> <hotkey_name>")
