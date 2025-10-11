# Async Debug Implementation Summary

This document summarizes the comprehensive async debugging solution implemented to diagnose PM2 event loop issues.

## What Was Implemented

### 1. Debug Mode Enablement
- Added `DEBUG_ASYNC` environment variable support in `neurons/miner.py`
- Can be toggled with `METAHASH_DEBUG_ASYNC=1`

### 2. Async Introspection Helper (`metahash/utils/async_debug.py`)
- `loop_info_dict()`: Captures comprehensive event loop and thread context
- `short_tb()`: Provides stack traces for debugging call sites

### 3. Instrumented Payments System (`metahash/miner/payments.py`)
- **Enhanced `submit()` method**: Logs every task submission with loop status and stack traces
- **Instrumented `start_background_tasks()`**: Logs loop status when starting background tasks
- **Heartbeats in watchdog**: Periodic logging every 10 iterations to prove it's running
- **Heartbeats in payment workers**: Logs every 5 attempts to show worker activity

### 4. Loop Status Logging in Miner Lifecycle (`neurons/miner.py`)
- **Before background tasks**: Logs loop status before payments are started
- **After background tasks**: Logs loop status after payments are started
- **In async handlers**: Logs loop status at entry to `auctionstart_forward()` and `win_forward()`
- **Guarded sync calls**: Logs when scheduling tasks from sync code (like `__init__`)

### 5. Chain Helper Debugging (`metahash/miner/runtime.py`)
- **Runtime.get_current_block()**: Logs loop status at entry to prove chain operations

## How to Test

### Debug Mode is Now Enabled by Default! 🎉
No environment variables needed - async debug logging is automatically enabled.

### Run with PM2 (Current Issue Scenario)
```bash
pm2 start neurons/miner.py --interpreter python3 --name metahash-miner
```

### Run Without PM2 (Control Scenario)
```bash
python3 neurons/miner.py
```

### Disable Debug Mode (if needed)
```bash
export METAHASH_DEBUG_ASYNC=0
# or
export METAHASH_DEBUG_ASYNC=false
```

## What You Should See

### Under Plain Execution (No PM2)
- `miner.before_bg` and `miner.after_bg` show `has_running_loop=True`
- Handler entry logs show `has_running_loop=True`
- "watchdog started" + periodic "watchdog heartbeat"
- Each payment worker logs "worker start" and occasional "worker tick"

### Under PM2 (Current Issue)
- Early logs show `has_running_loop=False` in `miner.before_bg`
- First `submit()` call from init prints "submit(): no running event loop" with stack trace
- After first RPC (auction/win), handler entry logs show `has_running_loop=True`
- Subsequent `submit()` calls succeed and payments begin

## Key Debug Points

1. **Loop Status at Initialization**: Shows whether event loop exists during `__init__`
2. **Task Submission Failures**: Proves when `submit()` fails due to no running loop
3. **Background Task Startup**: Shows loop status when background tasks are started
4. **Handler Entry Points**: Proves when event loop becomes available (after first RPC)
5. **Heartbeats**: Confirms background coroutines are actually running
6. **Chain Operations**: Shows loop status during blockchain operations

## Expected Root Cause Discovery

The logs should reveal that:
1. During `__init__`, there's no running event loop (`has_running_loop=False`)
2. Early `submit()` calls fail with "no running event loop" error
3. After the first RPC call, the event loop becomes available
4. Background tasks start successfully after the event loop is established

This will prove that the issue is PM2 not providing an event loop during initialization, and the solution is to defer background task startup until after the first RPC call establishes the event loop.
