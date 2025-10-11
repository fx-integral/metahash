module.exports = {
  apps: [{
    name: 'autoppia',
    script: 'neurons/miner.py',
    args: [
      '--wallet.name', 'owner',
      '--wallet.hotkey', 'miner3',
      '--netuid', '73',  // Correct netuid for metahash
      '--subtensor.network', 'ws://91.99.168.13:11144',  // Match the endpoint from your logs
      '--subtensor.chain_endpoint', 'ws://91.99.168.13:11144',
      '--miner.bids.netuids', '36',  // Correct subnet ID from your logs
      '--miner.bids.amounts', '50',  // Match your bid amount
      '--miner.bids.discounts', '100',  // Match your discount
      '--miner.bids.validators', '5Dnkprjf9fUrvWq3ZfFP8WrUNSjQws6UoHkbDfR1bQK8pFhW',  // Match validator from logs
      '--logging.debug',
      '--axon.port', '15289',  // Match port from logs
      '--fresh'
    ],
    interpreter: '/home/usuario1/miniconda3/envs/metahash/bin/python',
    cwd: '/home/usuario1/metahash/metahash',
    instances: 1,
    exec_mode: 'fork',
    watch: false,
    max_memory_restart: '1G',
    env: {
      NODE_ENV: 'production',
      PYTHONUNBUFFERED: '1',
      PYTHONIOENCODING: 'utf-8',
      // WebSocket keepalive settings to prevent timeouts
      WEBSOCKET_KEEPALIVE_INTERVAL: '30',
      WEBSOCKET_KEEPALIVE_TIMEOUT: '60',
      // Bittensor-specific environment variables
      BT_SUBTENSOR_NETWORK: 'ws://91.99.168.13:11144',
      BT_CHAIN_ENDPOINT: 'ws://91.99.168.13:11144',
      // PM2-specific settings for better WebSocket handling
      PM2_SERVE_PATH: '.',
      PM2_SERVE_PORT: 8080
    },
    // PM2 configurations optimized for WebSocket connections
    kill_timeout: 10000,  // Increased timeout for graceful shutdown
    wait_ready: false,    // Disable wait_ready to prevent hanging
    listen_timeout: 30000, // Increased listen timeout
    // Logging configuration
    log_file: '/home/usuario1/.pm2/logs/autoppia-combined.log',
    out_file: '/home/usuario1/.pm2/logs/autoppia-out.log',
    error_file: '/home/usuario1/.pm2/logs/autoppia-error.log',
    log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
    merge_logs: true,
    // Restart configuration - more aggressive for WebSocket issues
    autorestart: true,
    max_restarts: 20,     // More restarts allowed
    min_uptime: '30s',    // Longer minimum uptime
    restart_delay: 2000,  // Shorter restart delay
    // Advanced configurations
    node_args: [],
    exec_interpreter: 'none',
    // Disable PM2's built-in process management that can interfere with WebSockets
    ignore_watch: ['node_modules', 'logs'],
    // Environment-specific settings
    env_production: {
      NODE_ENV: 'production',
      PYTHONUNBUFFERED: '1',
      WEBSOCKET_KEEPALIVE_INTERVAL: '30',
      WEBSOCKET_KEEPALIVE_TIMEOUT: '60'
    }
  }]
};
