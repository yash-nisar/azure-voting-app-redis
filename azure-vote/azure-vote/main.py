# UI Configurations
TITLE = 'Azure Voting App'
VOTE1VALUE = 'Cats'
VOTE2VALUE = 'Dogs'
SHOWHOST = 'false'

from flask import Flask, request, render_template, jsonify # Added jsonify for health check
import os
import random
import redis
import socket
import sys
import logging
import time # Import time for potential delays/backoff if needed

# --- Setup Logging ---
# (Logging setup remains the same as before)
log_level_str = os.environ.get('LOG_LEVEL', 'INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)
logging.basicConfig(stream=sys.stdout, 
                    level=log_level,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- Configuration Loading ---
# (Configuration loading remains the same)
config_file = os.environ.get('CONFIG_FILE', 'config_file.cfg')
if os.path.exists(config_file):
    try:
        app.config.from_pyfile(config_file)
        logger.info(f"Loaded configuration from {config_file}")
    except Exception as e:
        logger.error(f"Failed to load configuration from {config_file}: {e}")
else:
    logger.warning(f"Configuration file {config_file} not found. Using defaults or environment variables.")
    app.config.setdefault('VOTE1VALUE', VOTE1VALUE)
    app.config.setdefault('VOTE2VALUE', VOTE2VALUE)
    app.config.setdefault('TITLE', TITLE)
    app.config.setdefault('SHOWHOST', SHOWHOST)

button1 = os.environ.get('VOTE1VALUE', app.config['VOTE1VALUE'])
button2 = os.environ.get('VOTE2VALUE', app.config['VOTE2VALUE'])
title = os.environ.get('TITLE', app.config['TITLE'])
show_host_str = os.environ.get('SHOWHOST', app.config['SHOWHOST']).lower()

# --- Redis Connection ---
redis_server = os.environ.get('REDIS')
redis_password = os.environ.get('REDIS_PWD')
redis_conn = None # Initialize connection variable

def get_redis_connection():
    """
    Establishes or retrieves the Redis connection.
    Includes basic retry logic for initial connection.
    """
    global redis_conn
    if redis_conn:
        try:
            # Quick check if the existing connection is likely valid
            redis_conn.ping() 
            return redis_conn
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
            logger.warning("Existing Redis connection failed ping. Attempting to reconnect.")
            redis_conn = None # Force reconnect attempt
        except redis.RedisError as e:
             logger.error(f"Unexpected Redis error on existing connection check: {e}. Attempting to reconnect.")
             redis_conn = None # Force reconnect attempt


    if not redis_server:
        logger.critical("REDIS environment variable not set.")
        # Cannot operate without redis server defined, return None
        return None

    logger.info(f"Attempting to establish new Redis connection to: {redis_server}")
    redis_connection_args = {
        'host': redis_server,
        'port': 6379,
        'socket_connect_timeout': 5, # Timeout for establishing connection
        'socket_timeout': 5,        # Timeout for operations
        'decode_responses': True,
        'health_check_interval': 30 # Check connection health periodically
    }
    if redis_password:
        logger.debug("Using password for Redis connection.") # Downgrade log level
        redis_connection_args['password'] = redis_password
    
    try:
        # Use StrictRedis which is the standard now (Redis = StrictRedis)
        new_conn = redis.StrictRedis(**redis_connection_args)
        new_conn.ping()
        logger.info("Successfully connected and pinged Redis server.")
        redis_conn = new_conn # Store the successful connection globally
        return redis_conn
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
        logger.error(f"Failed to connect to Redis (host: {redis_server}): {e}")
        return None
    except redis.exceptions.AuthenticationError as e:
        logger.critical(f"Redis authentication failed: {e}. Check REDIS_PWD.")
        # Authentication error is critical and unlikely to resolve on retry
        return None
    except redis.RedisError as e:
        logger.error(f"Redis error during connection attempt: {e}")
        return None

# --- Attempt Initial Connection & Initialization ---
# Try to connect at startup, but don't exit if it fails initially.
# Readiness probe will handle taking the pod out of service.
r_conn_initial = get_redis_connection()

if r_conn_initial:
    logger.info("Initial Redis connection successful.")
    # --- Initialize Redis Keys if they don't exist ---
    try:
        # Use the established connection
        if r_conn_initial.setnx(button1, 0):
           logger.info(f"Initialized Redis key '{button1}' to 0.")
        if r_conn_initial.setnx(button2, 0):
           logger.info(f"Initialized Redis key '{button2}' to 0.")
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
         logger.error(f"Redis connection error during initial key setup: {e}. App will continue but keys might not be set.")
    except redis.RedisError as e:
        logger.error(f"Redis error during initial key setup: {e}. App will continue but keys might not be set.")
else:
    logger.warning("Initial Redis connection failed. The application will start, but will be marked as not ready until Redis is available.")


# --- Optional: Change title to host name ---
if show_host_str == "true":
    try:
        hostname = socket.gethostname()
        title = f"{title} (served by {hostname})"
        logger.info(f"Displaying hostname in title: {hostname}")
    except socket.gaierror:
         logger.warning("Could not get hostname.")


# --- Helper function to execute Redis commands with error handling ---
def execute_redis_command(command, *args):
    """
    Executes a Redis command with connection handling and error catching.
    Returns the result or None if an error occurs.
    """
    r_conn = get_redis_connection() # Get potentially fresh connection
    if not r_conn:
        logger.error("Cannot execute Redis command: No connection available.")
        return None, "No Redis Connection" # Return None and an error indicator

    try:
        # Dynamically call the command on the connection object
        result = getattr(r_conn, command)(*args)
        logger.debug(f"Redis command '{command}' executed successfully.")
        return result, None # Return result and no error
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
        logger.error(f"Redis connection error executing command '{command}': {e}")
        # Invalidate the global connection object if a connection error occurs
        global redis_conn
        redis_conn = None 
        return None, f"Redis Connection Error: {e}"
    except redis.RedisError as e:
        logger.error(f"Redis error executing command '{command}': {e}")
        return None, f"Redis Error: {e}"


# --- Flask Routes ---
@app.route('/', methods=['GET', 'POST'])
def index():
    vote1, vote2 = 0, 0
    error_message = None

    if request.method == 'GET':
        logger.debug(f"Processing GET request from {request.remote_addr}")

        # Get current values using the helper
        val1_res, err1 = execute_redis_command('get', button1)
        val2_res, err2 = execute_redis_command('get', button2)

        if err1 or err2:
            error_message = "Error retrieving vote counts from database."
            logger.warning(f"GET request failed to retrieve votes. Errors: {err1}, {err2}")
            # Keep vote1, vote2 as 0 (default)
        else:
            # Ensure results are not None before converting to int
            vote1 = int(val1_res) if val1_res is not None else 0
            vote2 = int(val2_res) if val2_res is not None else 0
            logger.info(f"Successfully retrieved votes: {button1}={vote1}, {button2}={vote2}")

    elif request.method == 'POST':
        vote = request.form.get('vote')
        logger.info(f"Processing POST request from {request.remote_addr} with vote='{vote}'")

        # Always try to get current values first for display, even if POST fails
        val1_res, err1 = execute_redis_command('get', button1)
        val2_res, err2 = execute_redis_command('get', button2)
        vote1 = int(val1_res) if val1_res is not None and not err1 else 0
        vote2 = int(val2_res) if val2_res is not None and not err2 else 0
        if err1 or err2:
             # Log this, but don't set the main error_message yet, let the POST action try
             logger.warning(f"POST request failed to retrieve current votes before action. Errors: {err1}, {err2}")


        if not vote:
             logger.warning("POST request received without 'vote' data.")
             # Render with potentially stale data fetched above
             error_message = "Invalid request."

        elif vote == 'reset':
            logger.info(f"Reset vote request received from {request.remote_addr}.")
            _, err_set1 = execute_redis_command('set', button1, 0)
            _, err_set2 = execute_redis_command('set', button2, 0)

            if err_set1 or err_set2:
                error_message = "Database error: Failed to reset votes."
                logger.error(f"Failed to reset votes. Errors: {err_set1}, {err_set2}")
                # Keep vote1, vote2 as fetched above (potentially stale)
            else:
                logger.info(f"Successfully reset votes for '{button1}' and '{button2}' to 0.")
                vote1, vote2 = 0, 0 # Update local vars to reflect successful reset

        elif vote in [button1, button2]:
            logger.info(f"Vote cast for '{vote}' from {request.remote_addr}.")
            new_count_res, err_incr = execute_redis_command('incr', vote)

            if err_incr:
                error_message = f"Database error: Failed to record vote for '{vote}'."
                logger.error(f"Failed to increment vote for '{vote}'. Error: {err_incr}")
                 # Keep vote1, vote2 as fetched above (potentially stale)
            else:
                logger.info(f"Successfully incremented vote for '{vote}' to {new_count_res}.")
                # Fetch again after successful increment to get both counts
                val1_res, err1 = execute_redis_command('get', button1)
                val2_res, err2 = execute_redis_command('get', button2)
                vote1 = int(val1_res) if val1_res is not None and not err1 else 0
                vote2 = int(val2_res) if val2_res is not None and not err2 else 0
                if err1 or err2:
                    logger.warning(f"Vote incremented, but failed to retrieve updated counts. Errors: {err1}, {err2}")
                    error_message = "Vote recorded, but failed to display updated counts."


        else:
             logger.warning(f"Invalid vote value received in POST request: '{vote}'")
             error_message = "Invalid vote option selected."
             # Render with potentially stale data fetched above

    # Render template with current values and any error message
    return render_template("index.html",
                           value1=vote1,
                           value2=vote2,
                           button1=button1,
                           button2=button2,
                           title=title,
                           error_message=error_message) # Pass error message to template


@app.route('/healthz/live')
def liveness_check():
    """ Liveness probe: Checks if the Flask app itself is running. """
    # Basic check, always returns OK if the app is serving requests
    logger.debug("Liveness probe check successful.")
    return jsonify(status="UP"), 200


@app.route('/healthz/ready')
def readiness_check():
    """ Readiness probe: Checks if the app is ready to serve traffic (i.e., can connect to Redis). """
    logger.debug("Performing readiness probe check...")
    r_conn = get_redis_connection() # Attempt to get/establish connection
    if r_conn:
         try:
             r_conn.ping()
             logger.debug("Readiness probe check successful (Redis ping OK).")
             return jsonify(status="UP"), 200
         except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
              logger.warning(f"Readiness probe failed: Redis connection error on ping: {e}")
              global redis_conn # Invalidate connection on failure during probe
              redis_conn = None
              return jsonify(status="DOWN", reason=f"Redis connection error: {e}"), 503 # 503 Service Unavailable
         except redis.RedisError as e:
              logger.warning(f"Readiness probe failed: Redis error on ping: {e}")
              return jsonify(status="DOWN", reason=f"Redis error: {e}"), 503
    else:
        logger.warning("Readiness probe failed: Could not get Redis connection.")
        return jsonify(status="DOWN", reason="Cannot connect to Redis"), 503


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 80))
    logger.info(f"Starting Flask development server on host 0.0.0.0 port {port}")
    # Turn off debug for production/uwsgi environments
    # Use threaded=False if relying solely on uWSGI/Gunicorn for concurrency
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True) 