from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import mysql.connector
import requests
import os
from werkzeug.security import generate_password_hash, check_password_hash

# Pages are served by explicit routes below; no static folder, so source files are never exposed over HTTP.
app = Flask(__name__, static_folder=None)
CORS(app)

# Connection settings come from the environment (see .env.example); nothing secret is stored in code.
DB_CONFIG = {
    "host": os.environ.get("RIDEFLOW_DB_HOST", "localhost"),
    "port": int(os.environ.get("RIDEFLOW_DB_PORT", "3306")),
    "user": os.environ.get("RIDEFLOW_DB_USER", "root"),
    "password": os.environ.get("RIDEFLOW_DB_PASSWORD", ""),
    "auth_plugin": "mysql_native_password"
    # No "database" here
}
DB_NAME = "rideflow_db"  # also named in SCHEMA and the GRANT statements below

def get_db(use_db=True):
    config = DB_CONFIG.copy()
    if use_db:
        config["database"] = DB_NAME
    return mysql.connector.connect(**config)

def query(sql, params=None, fetch=True, many=False):
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute(sql, params or ())
    if fetch:
        result = cur.fetchall()
        conn.close()
        return result
    else:
        conn.commit()
        last_id = cur.lastrowid
        conn.close()
        return last_id

def run_fare_proc(vehicle_type, distance_km, duration_min, promo_code=''):
    conn = get_db()
    cur = conn.cursor(dictionary=True)

    # Call the stored procedure using a SET + CALL + SELECT pattern
    try:
        cur.execute("SET @v_fare = 0, @v_discount = 0")
        cur.execute(
            "CALL calculate_fare(%s, %s, %s, %s, @v_fare, @v_discount)",
            (vehicle_type, float(distance_km), int(duration_min), promo_code or '')
        )
        # Consume any result sets so the connection stays clean
        while cur.nextset():
            pass
        cur.execute("SELECT @v_fare AS final_fare, @v_discount AS discount")
        row = cur.fetchone()
        conn.close()
        if row and row['final_fare'] is not None:
            return float(row['final_fare']), float(row['discount'] or 0)
    except Exception as e:
        print(f"[run_fare_proc] error: {e}")
        conn.close()

    return 0.0, 0.0

def parse_coord(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def geocode_address(address):
    if not address:
        return None
    url = "https://nominatim.openstreetmap.org/search"
    params = {"format": "json", "limit": 1, "q": address}
    headers = {"User-Agent": "RideFlow/1.0"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=8)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    data = resp.json()
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])

def resolve_coords(data, prefix):
    lat = parse_coord(data.get(f"{prefix}_lat"))
    lng = parse_coord(data.get(f"{prefix}_lng"))
    if lat is not None and lng is not None:
        return lat, lng
    address = data.get(prefix) or data.get(f"{prefix}_location")
    return geocode_address(address)

def route_distance(pickup, dropoff):
    if not pickup or not dropoff:
        return None
    url = f"https://router.project-osrm.org/route/v1/driving/{pickup[1]},{pickup[0]};{dropoff[1]},{dropoff[0]}"
    params = {"overview": "false"}
    try:
        resp = requests.get(url, params=params, timeout=8)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    route = data["routes"][0]
    distance_km = route["distance"] / 1000.0
    duration_min = route["duration"] / 60.0
    return distance_km, duration_min

# ─── SETUP: CREATE TABLES ─────────────────────────────────────────────────────
SCHEMA = [
    "CREATE DATABASE IF NOT EXISTS rideflow_db",
    "USE rideflow_db",
    """
    CREATE TABLE IF NOT EXISTS Users (
        user_id INT AUTO_INCREMENT PRIMARY KEY,
        full_name VARCHAR(100) NOT NULL,
        email VARCHAR(100) UNIQUE NOT NULL,
        phone VARCHAR(20),
        password_hash VARCHAR(255),
        wallet_balance DECIMAL(10,2) DEFAULT 0.00,
        role ENUM('admin','rider','driver') DEFAULT 'rider',
        account_status ENUM('Active','Suspended','Banned') DEFAULT 'Active',
        registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS Drivers (
        driver_id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL,
        license_number VARCHAR(50),
        cnic VARCHAR(20),
        verification_status ENUM('Pending','Verified','Rejected') DEFAULT 'Pending',
        availability_status ENUM('Online','Offline','On Trip') DEFAULT 'Offline',
        total_trips INT DEFAULT 0,
        average_rating DECIMAL(3,2) DEFAULT 0.00,
        wallet_balance DECIMAL(10,2) DEFAULT 0.00,
        city VARCHAR(100),
        location VARCHAR(100),
        FOREIGN KEY (user_id) REFERENCES Users(user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS Vehicles (
        vehicle_id INT AUTO_INCREMENT PRIMARY KEY,
        driver_id INT NOT NULL,
        make VARCHAR(50),
        model VARCHAR(50),
        year INT,
        color VARCHAR(30),
        license_plate VARCHAR(20),
        vehicle_type ENUM('Economy','Premium','Bike') DEFAULT 'Economy',
        verification_status ENUM('Pending','Verified','Rejected') DEFAULT 'Pending',
        FOREIGN KEY (driver_id) REFERENCES Drivers(driver_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS Rides (
        ride_id INT AUTO_INCREMENT PRIMARY KEY,
        rider_id INT NOT NULL,
        driver_id INT,
        vehicle_id INT,
        pickup_location VARCHAR(200),
        dropoff_location VARCHAR(200),
        promo_code VARCHAR(30),
        preferred_fare DECIMAL(10,2),
        ride_status ENUM('Requested','Offered','Accepted','Driver En Route','In Progress','Rider Completed','Rider Cancelled','Completed','Cancelled') DEFAULT 'Requested',
        fare DECIMAL(10,2) DEFAULT 0.00,
        distance_km DECIMAL(6,2),
        duration_min INT,
        scheduled_time TIMESTAMP NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (rider_id) REFERENCES Users(user_id),
        FOREIGN KEY (driver_id) REFERENCES Drivers(driver_id),
        FOREIGN KEY (vehicle_id) REFERENCES Vehicles(vehicle_id)
    )
    """,
    "ALTER TABLE Rides MODIFY ride_status ENUM('Requested','Offered','Accepted','Driver En Route','In Progress','Rider Completed','Rider Cancelled','Completed','Cancelled') DEFAULT 'Requested'",
    """
    CREATE TABLE IF NOT EXISTS RideOffers (
        offer_id INT AUTO_INCREMENT PRIMARY KEY,
        ride_id INT NOT NULL,
        driver_id INT NOT NULL,
        vehicle_id INT NULL,
        proposed_fare DECIMAL(10,2) NOT NULL,
        status ENUM('Pending','Accepted','Rejected') DEFAULT 'Pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (ride_id) REFERENCES Rides(ride_id),
        FOREIGN KEY (driver_id) REFERENCES Drivers(driver_id),
        FOREIGN KEY (vehicle_id) REFERENCES Vehicles(vehicle_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS RideHistory (
        history_id INT AUTO_INCREMENT PRIMARY KEY,
        ride_id INT,
        rider_id INT,
        driver_id INT,
        pickup_location VARCHAR(200),
        dropoff_location VARCHAR(200),
        final_status ENUM('Completed','Cancelled'),
        fare DECIMAL(10,2),
        archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS FarePricing (
        pricing_id INT AUTO_INCREMENT PRIMARY KEY,
        vehicle_type ENUM('Economy','Premium','Bike'),
        base_rate DECIMAL(6,2),
        per_km_rate DECIMAL(6,2),
        per_min_rate DECIMAL(6,2),
        surge_multiplier DECIMAL(4,2) DEFAULT 1.00
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS PromoCodes (
        promo_id INT AUTO_INCREMENT PRIMARY KEY,
        code VARCHAR(30) UNIQUE,
        discount_percent DECIMAL(5,2),
        valid_until DATE,
        is_active BOOLEAN DEFAULT TRUE,
        usage_count INT DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS Payments (
        payment_id INT AUTO_INCREMENT PRIMARY KEY,
        ride_id INT NOT NULL,
        rider_id INT NOT NULL,
        amount DECIMAL(10,2),
        payment_method ENUM('Cash','Wallet','Card') DEFAULT 'Cash',
        payment_status ENUM('Pending','Paid','Failed','Refunded') DEFAULT 'Pending',
        transaction_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        promo_discount DECIMAL(10,2) DEFAULT 0.00,
        FOREIGN KEY (ride_id) REFERENCES Rides(ride_id),
        FOREIGN KEY (rider_id) REFERENCES Users(user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS Ratings (
        rating_id INT AUTO_INCREMENT PRIMARY KEY,
        ride_id INT NOT NULL,
        rated_by ENUM('Rider','Driver'),
        rated_user_id INT NOT NULL,
        score TINYINT CHECK (score BETWEEN 1 AND 5),
        comment TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (ride_id) REFERENCES Rides(ride_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS AdminFlags (
        flag_id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT,
        flag_reason VARCHAR(255),
        flagged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "ALTER TABLE Rides ADD COLUMN promo_code VARCHAR(30)",
    "ALTER TABLE Rides ADD COLUMN preferred_fare DECIMAL(10,2)",
    "ALTER TABLE Users ADD COLUMN wallet_balance DECIMAL(10,2) DEFAULT 0.00",
    "ALTER TABLE Drivers ADD COLUMN location VARCHAR(100)",
    "ALTER TABLE PromoCodes ADD COLUMN usage_count INT DEFAULT 0",
    "CREATE INDEX idx_rides_rider_id ON Rides(rider_id)",
    "CREATE INDEX idx_rides_driver_id ON Rides(driver_id)",
    "CREATE INDEX idx_rides_status ON Rides(ride_status)",
    "CREATE INDEX idx_drivers_city ON Drivers(city)",
    "DROP TRIGGER IF EXISTS flag_low_rated_rider",
    """
    CREATE TRIGGER flag_low_rated_rider
    AFTER INSERT ON Ratings
    FOR EACH ROW
    BEGIN
        DECLARE avg_r DECIMAL(3,2);
        IF NEW.rated_by = 'Driver' THEN
            SELECT AVG(score) INTO avg_r
            FROM Ratings
            WHERE rated_user_id = NEW.rated_user_id AND rated_by = 'Driver';
            IF avg_r < 3.0 THEN
                INSERT INTO AdminFlags(user_id, flag_reason)
                VALUES (NEW.rated_user_id, CONCAT('Rider average rating dropped to ', avg_r, ' — below 3.0 threshold'));
                UPDATE Users SET account_status = 'Suspended'
                WHERE user_id = NEW.rated_user_id AND account_status = 'Active';
            END IF;
        END IF;
    END
    """,
    "DROP TRIGGER IF EXISTS flag_low_rated_driver",
    """
    CREATE TRIGGER flag_low_rated_driver
    AFTER INSERT ON Ratings
    FOR EACH ROW
    BEGIN
        DECLARE avg_r DECIMAL(3,2);
        IF NEW.rated_by = 'Rider' THEN
            SELECT AVG(score) INTO avg_r FROM Ratings WHERE rated_user_id = NEW.rated_user_id AND rated_by = 'Rider';
            IF avg_r < 3.5 THEN
                INSERT INTO AdminFlags(user_id, flag_reason)
                VALUES (NEW.rated_user_id, CONCAT('Average driver rating dropped to ', avg_r));
            END IF;
            UPDATE Drivers SET average_rating = avg_r WHERE user_id = NEW.rated_user_id;
        END IF;
    END
    """,
    "DROP TRIGGER IF EXISTS trg_payment_complete_ride",
    """
    CREATE TRIGGER trg_payment_complete_ride
    AFTER INSERT ON Payments
    FOR EACH ROW
    BEGIN
        IF NEW.payment_status = 'Paid' THEN
            UPDATE Rides
            SET ride_status = 'Completed'
            WHERE ride_id = NEW.ride_id AND ride_status NOT IN ('Completed','Cancelled');
        END IF;
    END
    """,
    "DROP TRIGGER IF EXISTS trg_payment_complete_ride_update",
    """
    CREATE TRIGGER trg_payment_complete_ride_update
    AFTER UPDATE ON Payments
    FOR EACH ROW
    BEGIN
        IF NEW.payment_status = 'Paid' AND OLD.payment_status <> 'Paid' THEN
            UPDATE Rides
            SET ride_status = 'Completed'
            WHERE ride_id = NEW.ride_id AND ride_status NOT IN ('Completed','Cancelled');
        END IF;
    END
    """,
    "DROP TRIGGER IF EXISTS trg_promo_usage",
    """
    CREATE TRIGGER trg_promo_usage
    AFTER INSERT ON Payments
    FOR EACH ROW
    BEGIN
        DECLARE pcode VARCHAR(30);
        SELECT promo_code INTO pcode FROM Rides WHERE ride_id = NEW.ride_id;
        IF pcode IS NOT NULL AND pcode <> '' THEN
            UPDATE PromoCodes SET usage_count = usage_count + 1 WHERE code = pcode;
        END IF;
    END
    """,
    "DROP PROCEDURE IF EXISTS calculate_fare",
    """
    CREATE PROCEDURE calculate_fare(
        IN p_vehicle_type ENUM('Economy','Premium','Bike'),
        IN p_distance DECIMAL(6,2),
        IN p_duration INT,
        IN p_promo_code VARCHAR(30),
        OUT p_final_fare DECIMAL(10,2),
        OUT p_discount DECIMAL(10,2)
    )
    BEGIN
        DECLARE base DECIMAL(6,2);
        DECLARE per_km DECIMAL(6,2);
        DECLARE per_min DECIMAL(6,2);
        DECLARE surge DECIMAL(4,2);
        DECLARE raw_fare DECIMAL(10,2);
        DECLARE disc_pct DECIMAL(5,2) DEFAULT 0;
        DECLARE peak_mult DECIMAL(4,2) DEFAULT 1.00;
        DECLARE hr INT;

        SELECT base_rate, per_km_rate, per_min_rate, surge_multiplier
        INTO base, per_km, per_min, surge
        FROM FarePricing WHERE vehicle_type = p_vehicle_type LIMIT 1;

        SET hr = HOUR(NOW());
        IF (hr BETWEEN 7 AND 10) OR (hr BETWEEN 17 AND 20) THEN
            SET peak_mult = 1.25;
        ELSEIF (hr BETWEEN 22 AND 23) OR (hr BETWEEN 0 AND 5) THEN
            SET peak_mult = 1.10;
        END IF;

        SET raw_fare = (base + (per_km * p_distance) + (per_min * p_duration)) * surge * peak_mult;

        IF p_promo_code IS NOT NULL AND p_promo_code != '' THEN
            SELECT discount_percent INTO disc_pct FROM PromoCodes
            WHERE code = p_promo_code AND is_active = TRUE AND valid_until >= CURDATE()
            LIMIT 1;
        END IF;

        SET p_discount = raw_fare * (disc_pct / 100);
        SET p_final_fare = raw_fare - p_discount;
    END
    """,
    """
    CREATE OR REPLACE VIEW ActiveRidesView AS
    SELECT r.ride_id, r.ride_status, r.pickup_location, r.dropoff_location, r.created_at,
           u.full_name AS rider_name, u.phone AS rider_phone,
           du.full_name AS driver_name, du.phone AS driver_phone,
           v.make, v.model, v.license_plate, v.vehicle_type
    FROM Rides r
    JOIN Users u ON r.rider_id = u.user_id
    LEFT JOIN Drivers d ON r.driver_id = d.driver_id
    LEFT JOIN Users du ON d.user_id = du.user_id
    LEFT JOIN Vehicles v ON r.vehicle_id = v.vehicle_id
    WHERE r.ride_status NOT IN ('Completed','Cancelled')
    """,
    """
    CREATE OR REPLACE VIEW TopDriversView AS
    SELECT d.driver_id, u.full_name, d.city, d.average_rating, d.total_trips
    FROM Drivers d JOIN Users u ON d.user_id = u.user_id
    WHERE d.average_rating > 4.5
    ORDER BY d.average_rating DESC, d.total_trips DESC
    """,
    """
    CREATE OR REPLACE VIEW DriverLeaderboard AS
    SELECT d.driver_id, u.full_name, d.city, d.average_rating, d.total_trips
    FROM Drivers d JOIN Users u ON d.user_id = u.user_id
    WHERE d.verification_status = 'Verified'
    ORDER BY d.average_rating DESC, d.total_trips DESC
    """,
    """
    INSERT IGNORE INTO FarePricing(vehicle_type, base_rate, per_km_rate, per_min_rate, surge_multiplier)
    VALUES ('Economy', 50, 20, 2, 1.0),
           ('Premium', 100, 35, 4, 1.0),
           ('Bike', 30, 12, 1, 1.0)
    """,
    "DROP EVENT IF EXISTS expire_promos",
    """
    CREATE EVENT expire_promos
    ON SCHEDULE EVERY 1 DAY
    STARTS (TIMESTAMP(CURRENT_DATE, '00:00:00') + INTERVAL 1 DAY)
    DO
        UPDATE PromoCodes SET is_active = FALSE WHERE valid_until < CURDATE()
    """,
    "CREATE ROLE IF NOT EXISTS driver_role",
    "CREATE ROLE IF NOT EXISTS rider_role",
    "CREATE ROLE IF NOT EXISTS support_role",
    "CREATE ROLE IF NOT EXISTS admin_role",
    "GRANT SELECT ON rideflow_db.Rides TO driver_role",
    "GRANT SELECT, INSERT ON rideflow_db.Rides TO rider_role",
    "GRANT SELECT, INSERT ON rideflow_db.Payments TO rider_role",
    "REVOKE DELETE ON rideflow_db.* FROM support_role",
    "GRANT ALL PRIVILEGES ON rideflow_db.* TO admin_role"
]

@app.route('/api/setup', methods=['POST'])
def setup():
    conn = get_db(use_db=False)  # connect without database
    cur = conn.cursor()
    for stmt in SCHEMA:
        s = stmt.strip()
        if s:
            try:
                cur.execute(s)
            except Exception:
                pass
    conn.commit()
    conn.close()
    return jsonify({"message": "Database schema created successfully."})

# ─── USER MANAGEMENT ─────────────────────────────────────────────────────────
@app.route('/api/users', methods=['GET'])
def get_users():
    role = request.args.get('role', '')
    if role:
        return jsonify(query("SELECT user_id,full_name,email,phone,role,account_status,registration_date,wallet_balance FROM Users WHERE role=%s", (role,)))
    return jsonify(query("SELECT user_id,full_name,email,phone,role,account_status,registration_date,wallet_balance FROM Users"))

@app.route('/api/users', methods=['POST'])
def create_user():
    d = request.json
    lid = query(
        "INSERT INTO Users(full_name,email,phone,password_hash,role) VALUES(%s,%s,%s,%s,%s)",
        (d['full_name'], d['email'], d.get('phone'),
         # the admin form posts the plain password in "password_hash"; store only its hash
         generate_password_hash(d.get('password') or d.get('password_hash') or ''),
         d.get('role','rider')),
        fetch=False
    )
    return jsonify({"user_id": lid, "message": "User created"})

# ─── AUTH ───────────────────────────────────────────────────────────────────
@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    d = request.json or {}
    email = (d.get('email') or '').strip()
    password = d.get('password') or ''
    role = (d.get('role') or '').strip()
    if not email or not password or not role:
        return jsonify({"error": "email, password, and role are required"}), 400
    rows = query("SELECT user_id, full_name, role, password_hash, account_status FROM Users WHERE email=%s", (email,))
    if not rows:
        return jsonify({"error": "Invalid credentials"}), 401
    u = rows[0]
    if u['role'] != role:
        return jsonify({"error": "Invalid role for this account"}), 401
    if u['account_status'] in ('Banned', 'Suspended'):
        return jsonify({"error": f"Account {u['account_status'].lower()}"}), 403
    if not u['password_hash'] or not check_password_hash(u['password_hash'], password):
        return jsonify({"error": "Invalid credentials"}), 401
    return jsonify({"user_id": u['user_id'], "full_name": u['full_name'], "role": u['role']})

@app.route('/api/auth/signup', methods=['POST'])
def auth_signup():
    d = request.json or {}
    role = (d.get('role') or '').strip()
    full_name = (d.get('full_name') or '').strip()
    email = (d.get('email') or '').strip()
    password = d.get('password') or ''
    phone = d.get('phone')
    if role not in ('admin', 'rider', 'driver'):
        return jsonify({"error": "Invalid role"}), 400
    if not full_name or not email or not password:
        return jsonify({"error": "full_name, email, and password are required"}), 400
    if query("SELECT user_id FROM Users WHERE email=%s", (email,)):
        return jsonify({"error": "Email already registered"}), 400
    if role == 'driver':
        if not (d.get('license_number') and d.get('cnic')):
            return jsonify({"error": "license_number and cnic are required for drivers"}), 400
    uid = query(
        "INSERT INTO Users(full_name,email,phone,password_hash,role) VALUES(%s,%s,%s,%s,%s)",
        (full_name, email, phone, generate_password_hash(password), role),
        fetch=False
    )
    driver_id = None
    if role == 'driver':
        driver_id = query(
            "INSERT INTO Drivers(user_id,license_number,cnic,city) VALUES(%s,%s,%s,%s)",
            (uid, d.get('license_number'), d.get('cnic'), d.get('city','')),
            fetch=False
        )
    return jsonify({"user_id": uid, "driver_id": driver_id, "role": role, "message": "Signup successful"})

# ─── WALLET ─────────────────────────────────────────────────────────────────
@app.route('/api/wallet/<int:uid>', methods=['GET'])
def get_wallet(uid):
    rows = query("SELECT wallet_balance FROM Users WHERE user_id=%s", (uid,))
    if not rows:
        return jsonify({"error": "User not found"}), 404
    return jsonify({"user_id": uid, "wallet_balance": float(rows[0]['wallet_balance'] or 0)})

@app.route('/api/wallet/<int:uid>/topup', methods=['POST'])
def topup_wallet(uid):
    d = request.json or {}
    amount = float(d.get('amount', 0) or 0)
    if amount <= 0:
        return jsonify({"error": "amount must be greater than 0"}), 400
    query("UPDATE Users SET wallet_balance=wallet_balance+%s WHERE user_id=%s", (amount, uid), fetch=False)
    return jsonify({"message": "Wallet topped up", "amount": amount})

@app.route('/api/users/<int:uid>/status', methods=['PUT'])
def update_user_status(uid):
    d = request.json
    query("UPDATE Users SET account_status=%s WHERE user_id=%s", (d['account_status'], uid), fetch=False)
    return jsonify({"message": "Status updated"})

@app.route('/api/users/<int:uid>', methods=['DELETE'])
def delete_user(uid):
    rows = query("SELECT user_id, role FROM Users WHERE user_id=%s", (uid,))
    if not rows:
        return jsonify({"error": "User not found"}), 404
    blocks = []
    if query("SELECT COUNT(*) as cnt FROM Rides WHERE rider_id=%s", (uid,))[0]['cnt'] > 0:
        blocks.append("rides as rider")
    if query("SELECT COUNT(*) as cnt FROM Payments WHERE rider_id=%s", (uid,))[0]['cnt'] > 0:
        blocks.append("payments")
    if query("SELECT COUNT(*) as cnt FROM Ratings WHERE rated_user_id=%s", (uid,))[0]['cnt'] > 0:
        blocks.append("ratings")

    drows = query("SELECT driver_id FROM Drivers WHERE user_id=%s", (uid,))
    did = drows[0]['driver_id'] if drows else None
    if did:
        if query("SELECT COUNT(*) as cnt FROM Rides WHERE driver_id=%s", (did,))[0]['cnt'] > 0:
            blocks.append("rides as driver")

    if blocks:
        return jsonify({"error": "Cannot delete user. Remove related records first: " + ", ".join(blocks)}), 400

    if did:
        query("DELETE FROM Vehicles WHERE driver_id=%s", (did,), fetch=False)
        query("DELETE FROM Drivers WHERE driver_id=%s", (did,), fetch=False)
    query("DELETE FROM AdminFlags WHERE user_id=%s", (uid,), fetch=False)
    query("DELETE FROM Users WHERE user_id=%s", (uid,), fetch=False)
    return jsonify({"message": "User deleted"})

# ─── DRIVER MANAGEMENT ───────────────────────────────────────────────────────
@app.route('/api/drivers', methods=['GET'])
def get_drivers():
    return jsonify(query("""
        SELECT d.driver_id, d.user_id, u.full_name, u.email, d.license_number, d.cnic,
               d.verification_status, d.availability_status, d.total_trips,
               d.average_rating, d.wallet_balance, d.city
        FROM Drivers d JOIN Users u ON d.user_id=u.user_id
    """))

@app.route('/api/drivers', methods=['POST'])
def create_driver():
    d = request.json
    uid = d.get('user_id') if d else None
    if not uid:
        return jsonify({"error": "user_id is required"}), 400
    urows = query("SELECT user_id, role FROM Users WHERE user_id=%s", (uid,))
    if not urows:
        return jsonify({"error": "User not found. Create a user first."}), 400
    if urows[0]['role'] != 'driver':
        return jsonify({"error": "User role must be driver. Update user role or create a driver user."}), 400
    lid = query(
        "INSERT INTO Drivers(user_id,license_number,cnic,city) VALUES(%s,%s,%s,%s)",
        (d['user_id'], d['license_number'], d['cnic'], d.get('city','')),
        fetch=False
    )
    return jsonify({"driver_id": lid, "message": "Driver profile created"})

@app.route('/api/drivers/<int:did>/verify', methods=['PUT'])
def verify_driver(did):
    d = request.json
    query("UPDATE Drivers SET verification_status=%s WHERE driver_id=%s", (d['status'], did), fetch=False)
    return jsonify({"message": "Driver verification updated"})

@app.route('/api/drivers/<int:did>/availability', methods=['PUT'])
def driver_availability(did):
    d = request.json
    query("UPDATE Drivers SET availability_status=%s WHERE driver_id=%s", (d['availability_status'], did), fetch=False)
    return jsonify({"message": "Availability updated"})

@app.route('/api/drivers/<int:did>/payout', methods=['POST'])
def request_payout(did):
    rows = query("SELECT wallet_balance FROM Drivers WHERE driver_id=%s", (did,))
    if not rows:
        return jsonify({"error": "Driver not found"}), 404
    bal = rows[0]['wallet_balance']
    query("UPDATE Drivers SET wallet_balance=0 WHERE driver_id=%s", (did,), fetch=False)
    return jsonify({"message": f"Payout of PKR {bal} processed", "amount": float(bal)})

# ─── VEHICLE MANAGEMENT ──────────────────────────────────────────────────────
@app.route('/api/vehicles', methods=['GET'])
def get_vehicles():
    return jsonify(query("""
        SELECT v.*, u.full_name as driver_name, d.verification_status as driver_status, d.city
        FROM Vehicles v
        LEFT JOIN Drivers d ON v.driver_id = d.driver_id
        LEFT JOIN Users u ON d.user_id = u.user_id
        ORDER BY v.vehicle_id DESC
    """))

@app.route('/api/vehicles', methods=['POST'])
def create_vehicle():
    d = request.json
    lid = query(
        "INSERT INTO Vehicles(driver_id,make,model,year,color,license_plate,vehicle_type) VALUES(%s,%s,%s,%s,%s,%s,%s)",
        (d['driver_id'], d['make'], d['model'], d['year'], d['color'], d['license_plate'], d['vehicle_type']),
        fetch=False
    )
    return jsonify({"vehicle_id": lid, "message": "Vehicle registered"})

@app.route('/api/vehicles/<int:vid>/verify', methods=['PUT'])
def verify_vehicle(vid):
    d = request.json
    query("UPDATE Vehicles SET verification_status=%s WHERE vehicle_id=%s", (d['status'], vid), fetch=False)
    return jsonify({"message": "Vehicle verification updated"})

# ─── RIDE MANAGEMENT ─────────────────────────────────────────────────────────
@app.route('/api/rides', methods=['GET'])
def get_rides():
    status = request.args.get('status','')
    rider_id = request.args.get('rider_id')
    driver_id = request.args.get('driver_id')
    unassigned = request.args.get('unassigned')

    sql = """
        SELECT r.*, u.full_name as rider_name,
               du.user_id as driver_user_id, du.full_name as driver_name
        FROM Rides r
        JOIN Users u ON r.rider_id=u.user_id
        LEFT JOIN Drivers d ON r.driver_id=d.driver_id
        LEFT JOIN Users du ON d.user_id=du.user_id
    """
    conds = []
    params = []
    if status:
        statuses = [s.strip() for s in status.split(',') if s.strip()]
        if len(statuses) == 1:
            conds.append("r.ride_status=%s")
            params.append(statuses[0])
        elif statuses:
            conds.append("r.ride_status IN (" + ",".join(["%s"] * len(statuses)) + ")")
            params.extend(statuses)
    if rider_id:
        conds.append("r.rider_id=%s")
        params.append(rider_id)
    if driver_id:
        conds.append("r.driver_id=%s")
        params.append(driver_id)
    if unassigned:
        conds.append("r.driver_id IS NULL")

    city = request.args.get('city')
    if city:
        # Extract city from pickup_location using LIKE match
        conds.append("r.pickup_location LIKE %s")
        params.append(f"%{city}%")

    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY r.created_at DESC LIMIT 100"
    return jsonify(query(sql, tuple(params)))

@app.route('/api/rides', methods=['POST'])
def create_ride():
    d = request.json
    lid = query(
        "INSERT INTO Rides(rider_id,pickup_location,dropoff_location,promo_code,distance_km,duration_min,scheduled_time) VALUES(%s,%s,%s,%s,%s,%s,%s)",
        (d['rider_id'], d['pickup_location'], d['dropoff_location'],
         d.get('promo_code'), d.get('distance_km', 5), d.get('duration_min', 15), d.get('scheduled_time')),
        fetch=False
    )
    return jsonify({"ride_id": lid, "message": "Ride requested"})

def archive_ride(r, final_status):
    query(
        "INSERT INTO RideHistory(ride_id,rider_id,driver_id,pickup_location,dropoff_location,final_status,fare) VALUES(%s,%s,%s,%s,%s,%s,%s)",
        (r['ride_id'], r['rider_id'], r['driver_id'], r['pickup_location'], r['dropoff_location'], final_status, r['fare']),
        fetch=False
    )

def finalize_ride(ride_id, final_status):
    rows = query("SELECT * FROM Rides WHERE ride_id=%s", (ride_id,))
    if not rows:
        return
    if query("SELECT history_id FROM RideHistory WHERE ride_id=%s LIMIT 1", (ride_id,)):
        return
    r = rows[0]
    archive_ride(r, final_status)
    if final_status == 'Completed' and r.get('driver_id'):
        query("UPDATE Drivers SET total_trips=total_trips+1 WHERE driver_id=%s", (r['driver_id'],), fetch=False)

@app.route('/api/rides/<int:rid>/status', methods=['PUT'])
def update_ride_status(rid):
    d = request.json
    new_status = d['ride_status']
    query("UPDATE Rides SET ride_status=%s WHERE ride_id=%s", (new_status, rid), fetch=False)

    if new_status in ('Completed', 'Cancelled'):
        finalize_ride(rid, new_status)

    return jsonify({"message": f"Ride status updated to {new_status}"})

@app.route('/api/rides/<int:rid>/assign', methods=['PUT'])
def assign_driver(rid):
    d = request.json
    query("UPDATE Rides SET driver_id=%s, vehicle_id=%s, ride_status='Accepted' WHERE ride_id=%s",
          (d['driver_id'], d['vehicle_id'], rid), fetch=False)
    query("UPDATE Drivers SET availability_status='On Trip' WHERE driver_id=%s", (d['driver_id'],), fetch=False)
    return jsonify({"message": "Driver assigned"})

@app.route('/api/rides/<int:rid>/offer', methods=['POST'])
def offer_fare(rid):
    d = request.json or {}
    driver_id = d.get('driver_id')
    proposed_fare = d.get('proposed_fare')
    if not driver_id or proposed_fare is None:
        return jsonify({"error": "driver_id and proposed_fare are required"}), 400
    rides = query("SELECT ride_id, ride_status FROM Rides WHERE ride_id=%s", (rid,))
    if not rides:
        return jsonify({"error": "Ride not found"}), 404
    if rides[0]['ride_status'] in ('Completed', 'Cancelled', 'In Progress', 'Rider Completed', 'Rider Cancelled'):
        return jsonify({"error": "Ride already closed"}), 400
    if not query("SELECT driver_id FROM Drivers WHERE driver_id=%s", (driver_id,)):
        return jsonify({"error": "Driver not found"}), 404
    offer_id = query(
        "INSERT INTO RideOffers(ride_id,driver_id,vehicle_id,proposed_fare) VALUES(%s,%s,%s,%s)",
        (rid, driver_id, d.get('vehicle_id'), proposed_fare),
        fetch=False
    )
    query("UPDATE Rides SET ride_status='Offered' WHERE ride_id=%s", (rid,), fetch=False)
    return jsonify({"offer_id": offer_id, "message": "Offer submitted"})

@app.route('/api/rides/<int:rid>/reject', methods=['POST'])
def reject_ride(rid):
    d = request.json or {}
    driver_id = d.get('driver_id')
    if not driver_id:
        return jsonify({"error": "driver_id is required"}), 400
    rides = query("SELECT ride_id, ride_status FROM Rides WHERE ride_id=%s", (rid,))
    if not rides:
        return jsonify({"error": "Ride not found"}), 404
    if rides[0]['ride_status'] in ('Completed', 'Cancelled'):
        return jsonify({"error": "Ride already closed"}), 400
    if not query("SELECT driver_id FROM Drivers WHERE driver_id=%s", (driver_id,)):
        return jsonify({"error": "Driver not found"}), 404
    offer_id = query(
        "INSERT INTO RideOffers(ride_id,driver_id,vehicle_id,proposed_fare,status) VALUES(%s,%s,%s,%s,'Rejected')",
        (rid, driver_id, d.get('vehicle_id'), 0),
        fetch=False
    )
    return jsonify({"offer_id": offer_id, "message": "Ride rejected"})

@app.route('/api/rides/<int:rid>/offers', methods=['GET'])
def get_offers(rid):
    return jsonify(query("""
        SELECT o.*, u.full_name as driver_name
        FROM RideOffers o
        JOIN Drivers d ON o.driver_id=d.driver_id
        JOIN Users u ON d.user_id=u.user_id
        WHERE o.ride_id=%s
        ORDER BY o.created_at DESC
    """, (rid,)))

@app.route('/api/rides/<int:rid>/accept', methods=['POST'])
def accept_offer(rid):
    d = request.json or {}
    offer_id = d.get('offer_id')
    rider_id = d.get('rider_id')
    if not offer_id:
        return jsonify({"error": "offer_id is required"}), 400
    rides = query("SELECT * FROM Rides WHERE ride_id=%s", (rid,))
    if not rides:
        return jsonify({"error": "Ride not found"}), 404
    if rides[0]['ride_status'] in ('Completed', 'Cancelled'):
        return jsonify({"error": "Ride already closed"}), 400
    if rider_id and str(rides[0]['rider_id']) != str(rider_id):
        return jsonify({"error": "Unauthorized rider"}), 403
    offers = query("SELECT * FROM RideOffers WHERE offer_id=%s AND ride_id=%s", (offer_id, rid))
    if not offers:
        return jsonify({"error": "Offer not found"}), 404
    if offers[0]['status'] != 'Pending':
        return jsonify({"error": "Offer is no longer available"}), 400

    # Enforce: only verified vehicles can be used for active rides
    vehicle_id = offers[0].get('vehicle_id')
    if not vehicle_id:
        # Fallback: auto-assign the driver's verified vehicle if none attached to offer
        vfallback = query(
            "SELECT vehicle_id FROM Vehicles WHERE driver_id=%s AND verification_status='Verified' LIMIT 1",
            (offers[0]['driver_id'],)
        )
        if vfallback:
            vehicle_id = vfallback[0]['vehicle_id']
            query("UPDATE RideOffers SET vehicle_id=%s WHERE offer_id=%s", (vehicle_id, offer_id), fetch=False)
        else:
            return jsonify({"error": "Cannot start ride — no verified vehicle found for this driver"}), 400
    else:
        vrows = query("SELECT verification_status FROM Vehicles WHERE vehicle_id=%s", (vehicle_id,))
        if vrows and vrows[0]['verification_status'] != 'Verified':
            return jsonify({"error": "Cannot start ride — vehicle is not verified by admin yet"}), 400

    query("UPDATE RideOffers SET status='Accepted' WHERE offer_id=%s", (offer_id,), fetch=False)
    query("UPDATE RideOffers SET status='Rejected' WHERE ride_id=%s AND offer_id<>%s", (rid, offer_id), fetch=False)
    query(
        "UPDATE Rides SET driver_id=%s, vehicle_id=%s, fare=%s, ride_status='In Progress' WHERE ride_id=%s",
        (offers[0]['driver_id'], offers[0].get('vehicle_id'), offers[0]['proposed_fare'], rid),
        fetch=False
    )
    query("UPDATE Drivers SET availability_status='On Trip' WHERE driver_id=%s", (offers[0]['driver_id'],), fetch=False)
    return jsonify({"message": "Offer accepted. Ride started."})

@app.route('/api/rides/<int:rid>/rider-complete', methods=['PUT'])
def rider_complete(rid):
    rows = query("SELECT ride_status FROM Rides WHERE ride_id=%s", (rid,))
    if not rows:
        return jsonify({"error": "Ride not found"}), 404
    if rows[0]['ride_status'] != 'In Progress':
        return jsonify({"error": "Ride is not in progress"}), 400
    query("UPDATE Rides SET ride_status='Rider Completed' WHERE ride_id=%s", (rid,), fetch=False)
    return jsonify({"message": "Ride marked as completed. Waiting for driver acknowledgement."})

@app.route('/api/rides/<int:rid>/rider-cancel', methods=['PUT'])
def rider_cancel(rid):
    rows = query("SELECT ride_status, driver_id FROM Rides WHERE ride_id=%s", (rid,))
    if not rows:
        return jsonify({"error": "Ride not found"}), 404
    status = rows[0]['ride_status']
    if status in ('Completed', 'Cancelled'):
        return jsonify({"error": "Ride already closed"}), 400
    if status in ('Requested', 'Offered') and not rows[0]['driver_id']:
        query("UPDATE Rides SET ride_status='Cancelled' WHERE ride_id=%s", (rid,), fetch=False)
        finalize_ride(rid, 'Cancelled')
        return jsonify({"message": "Ride cancelled"})
    query("UPDATE Rides SET ride_status='Rider Cancelled' WHERE ride_id=%s", (rid,), fetch=False)
    return jsonify({"message": "Ride cancelled. Waiting for driver acknowledgement."})

@app.route('/api/rides/<int:rid>/driver-ack', methods=['PUT'])
def driver_ack(rid):
    rows = query("SELECT ride_status, driver_id, rider_id, fare FROM Rides WHERE ride_id=%s", (rid,))
    if not rows:
        return jsonify({"error": "Ride not found"}), 404
    status = rows[0]['ride_status']
    driver_id = rows[0]['driver_id']
    rider_id  = rows[0]['rider_id']
    fare      = float(rows[0]['fare'] or 0)

    if status == 'Rider Completed':
        query("UPDATE Rides SET ride_status='Completed' WHERE ride_id=%s", (rid,), fetch=False)
        finalize_ride(rid, 'Completed')

        # ── Check if a payment record already exists for this ride ──
        existing = query("SELECT payment_id, payment_status FROM Payments WHERE ride_id=%s LIMIT 1", (rid,))

        if not existing:
            # No payment yet — create one and handle wallet deduction
            rider_rows = query("SELECT wallet_balance FROM Users WHERE user_id=%s", (rider_id,))
            wallet = float(rider_rows[0]['wallet_balance'] or 0) if rider_rows else 0

            if wallet >= fare:
                # Deduct from rider wallet
                query("UPDATE Users SET wallet_balance = wallet_balance - %s WHERE user_id=%s",
                      (fare, rider_id), fetch=False)
                method = 'Wallet'
                status_val = 'Paid'
            else:
                # Cash payment — no deduction needed
                method = 'Cash'
                status_val = 'Paid'

            query("""INSERT INTO Payments(ride_id, rider_id, amount, payment_method, payment_status, promo_discount)
                     VALUES(%s, %s, %s, %s, %s, 0)""",
                  (rid, rider_id, fare, method, status_val), fetch=False)

        elif existing[0]['payment_status'] != 'Paid':
            # Payment exists but unpaid — mark it paid and deduct wallet
            rider_rows = query("SELECT wallet_balance FROM Users WHERE user_id=%s", (rider_id,))
            wallet = float(rider_rows[0]['wallet_balance'] or 0) if rider_rows else 0
            if wallet >= fare:
                query("UPDATE Users SET wallet_balance = wallet_balance - %s WHERE user_id=%s",
                      (fare, rider_id), fetch=False)
            query("UPDATE Payments SET payment_status='Paid' WHERE payment_id=%s",
                  (existing[0]['payment_id'],), fetch=False)

        # ── Credit driver: 80% of fare after 20% platform commission ──
        if driver_id and fare > 0:
            net = round(fare * 0.80, 2)
            query("UPDATE Drivers SET wallet_balance = wallet_balance + %s, total_trips = total_trips + 1 WHERE driver_id=%s",
                  (net, driver_id), fetch=False)

    elif status == 'Rider Cancelled':
        query("UPDATE Rides SET ride_status='Cancelled' WHERE ride_id=%s", (rid,), fetch=False)
        finalize_ride(rid, 'Cancelled')
    else:
        return jsonify({"error": "Ride is not awaiting acknowledgement"}), 400

    if driver_id:
        query("UPDATE Drivers SET availability_status='Online' WHERE driver_id=%s", (driver_id,), fetch=False)

    return jsonify({"message": "Acknowledged"})

@app.route('/api/ride-history', methods=['GET'])
def ride_history():
    rider_id = request.args.get('rider_id')
    driver_id = request.args.get('driver_id')
    sql = "SELECT * FROM RideHistory"
    conds = []
    params = []
    if rider_id:
        conds.append("rider_id=%s")
        params.append(rider_id)
    if driver_id:
        conds.append("driver_id=%s")
        params.append(driver_id)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY archived_at DESC LIMIT 100"
    return jsonify(query(sql, tuple(params)))

# ─── FARE & PAYMENT ──────────────────────────────────────────────────────────
@app.route('/api/fare/calculate', methods=['POST'])
def calc_fare():
    d = request.json
    final_fare, discount = run_fare_proc(
        d.get('vehicle_type','Economy'),
        float(d.get('distance_km', 5)),
        int(d.get('duration_min', 15)),
        d.get('promo_code', '')
    )
    return jsonify({"final_fare": final_fare, "discount": discount})

@app.route('/api/geo/route', methods=['POST'])
def geo_route():
    d = request.json or {}
    pickup = resolve_coords(d, 'pickup')
    dropoff = resolve_coords(d, 'dropoff')
    if not pickup or not dropoff:
        return jsonify({"error": "Could not resolve pickup/dropoff coordinates"}), 400
    route = route_distance(pickup, dropoff)
    if not route:
        return jsonify({"error": "Routing service unavailable"}), 502
    distance_km, duration_min = route
    return jsonify({
        "pickup_lat": pickup[0],
        "pickup_lng": pickup[1],
        "dropoff_lat": dropoff[0],
        "dropoff_lng": dropoff[1],
        "distance_km": round(distance_km, 3),
        "duration_min": round(duration_min, 1)
    })

@app.route('/api/fare/recommend', methods=['POST'])
def recommend_fare():
    d = request.json or {}
    vehicle_type = d.get('vehicle_type', 'Economy')
    promo_code = d.get('promo_code', '')
    distance_km = d.get('distance_km')
    duration_min = d.get('duration_min')

    if distance_km is None or duration_min is None:
        pickup = resolve_coords(d, 'pickup')
        dropoff = resolve_coords(d, 'dropoff')
        if not pickup or not dropoff:
            return jsonify({"error": "Provide distance/duration or pickup/dropoff"}), 400
        route = route_distance(pickup, dropoff)
        if not route:
            return jsonify({"error": "Routing service unavailable"}), 502
        distance_km, duration_min = route

    final_fare, discount = run_fare_proc(vehicle_type, float(distance_km), int(round(float(duration_min))), promo_code)
    return jsonify({
        "vehicle_type": vehicle_type,
        "distance_km": round(float(distance_km), 3),
        "duration_min": round(float(duration_min), 1),
        "final_fare": final_fare,
        "discount": discount
    })

@app.route('/api/payments', methods=['GET'])
def get_payments():
    return jsonify(query("SELECT * FROM Payments ORDER BY transaction_date DESC LIMIT 50"))

@app.route('/api/payments', methods=['POST'])
def create_payment():
    d = request.json
    # Get fare
    rides = query("SELECT fare, driver_id, ride_status FROM Rides WHERE ride_id=%s", (d['ride_id'],))
    if not rides:
        return jsonify({"error": "Ride not found"}), 404
    fare = float(rides[0]['fare'] or 0)
    promo_disc = float(d.get('promo_discount', 0))
    amount = fare - promo_disc
    method = d.get('payment_method', 'Cash')
    if method == 'Wallet':
        wrows = query("SELECT wallet_balance FROM Users WHERE user_id=%s", (d['rider_id'],))
        if not wrows:
            return jsonify({"error": "Rider not found"}), 404
        if float(wrows[0]['wallet_balance'] or 0) < amount:
            return jsonify({"error": "Insufficient wallet balance"}), 400
        query("UPDATE Users SET wallet_balance=wallet_balance-%s WHERE user_id=%s", (amount, d['rider_id']), fetch=False)

    lid = query(
        "INSERT INTO Payments(ride_id,rider_id,amount,payment_method,payment_status,promo_discount) VALUES(%s,%s,%s,%s,'Paid',%s)",
        (d['ride_id'], d['rider_id'], amount, method, promo_disc),
        fetch=False
    )

    if rides[0]['ride_status'] not in ('Completed', 'Cancelled'):
        query("UPDATE Rides SET ride_status='Completed' WHERE ride_id=%s", (d['ride_id'],), fetch=False)
        finalize_ride(d['ride_id'], 'Completed')

    # Credit driver earnings (80% after 20% commission)
    driver_id = rides[0]['driver_id']
    if driver_id:
        net = amount * 0.80
        query("UPDATE Drivers SET wallet_balance=wallet_balance+%s WHERE driver_id=%s", (net, driver_id), fetch=False)

    return jsonify({"payment_id": lid, "amount_paid": amount, "message": "Payment recorded"})

@app.route('/api/payments/<int:pid>/refund', methods=['PUT'])
def refund_payment(pid):
    query("UPDATE Payments SET payment_status='Refunded' WHERE payment_id=%s", (pid,), fetch=False)
    return jsonify({"message": "Payment refunded"})

@app.route('/api/pricing', methods=['GET'])
def get_pricing():
    return jsonify(query("SELECT * FROM FarePricing"))

@app.route('/api/pricing/<int:pid>', methods=['PUT'])
def update_pricing(pid):
    d = request.json
    query("UPDATE FarePricing SET base_rate=%s,per_km_rate=%s,per_min_rate=%s,surge_multiplier=%s WHERE pricing_id=%s",
          (d['base_rate'], d['per_km_rate'], d['per_min_rate'], d['surge_multiplier'], pid), fetch=False)
    return jsonify({"message": "Pricing updated"})

# ─── PROMO CODES ─────────────────────────────────────────────────────────────
@app.route('/api/promos', methods=['GET'])
def get_promos():
    return jsonify(query("SELECT * FROM PromoCodes"))

@app.route('/api/promos', methods=['POST'])
def create_promo():
    d = request.json
    lid = query(
        "INSERT INTO PromoCodes(code,discount_percent,valid_until) VALUES(%s,%s,%s)",
        (d['code'], d['discount_percent'], d['valid_until']),
        fetch=False
    )
    return jsonify({"promo_id": lid, "message": "Promo code created"})

# ─── RATINGS ─────────────────────────────────────────────────────────────────
@app.route('/api/ratings', methods=['GET'])
def get_ratings():
    return jsonify(query("SELECT * FROM Ratings ORDER BY created_at DESC LIMIT 50"))

@app.route('/api/ratings', methods=['POST'])
def create_rating():
    d = request.json
    lid = query(
        "INSERT INTO Ratings(ride_id,rated_by,rated_user_id,score,comment) VALUES(%s,%s,%s,%s,%s)",
        (d['ride_id'], d['rated_by'], d['rated_user_id'], d['score'], d.get('comment','')),
        fetch=False
    )
    return jsonify({"rating_id": lid, "message": "Rating submitted"})

# ─── BASIC QUERIES ──────────────────────────────────────────────────────────
@app.route('/api/queries/completed-rides', methods=['GET'])
def completed_rides_by_rider():
    rider_id = request.args.get('rider_id')
    if not rider_id:
        return jsonify({"error": "rider_id is required"}), 400
    return jsonify(query("""
        SELECT r.ride_id, r.pickup_location, r.dropoff_location, r.fare, r.created_at
        FROM Rides r
        WHERE r.rider_id=%s AND r.ride_status='Completed'
        ORDER BY r.created_at DESC
    """, (rider_id,)))

@app.route('/api/queries/drivers-by-city', methods=['GET'])
def drivers_by_city():
    city = request.args.get('city')
    if not city:
        return jsonify({"error": "city is required"}), 400
    return jsonify(query("""
        SELECT d.driver_id, u.full_name, d.city, d.average_rating, d.total_trips
        FROM Drivers d JOIN Users u ON d.user_id=u.user_id
        WHERE d.city=%s
        ORDER BY d.average_rating DESC
    """, (city,)))

# ─── REPORTS ─────────────────────────────────────────────────────────────────
@app.route('/api/reports/revenue-by-city', methods=['GET'])
def revenue_by_city():
    return jsonify(query("""
        SELECT d.city, SUM(p.amount) as total_revenue, COUNT(*) as transaction_count
        FROM Payments p
        JOIN Rides r ON p.ride_id=r.ride_id
        JOIN Drivers d ON r.driver_id=d.driver_id
        WHERE p.payment_status='Paid'
        GROUP BY d.city
        ORDER BY total_revenue DESC
    """))

@app.route('/api/reports/low-rated-drivers', methods=['GET'])
def low_rated_drivers():
    return jsonify(query("""
        SELECT d.driver_id, u.full_name, d.city, AVG(rt.score) as avg_rating, COUNT(*) as rating_count
        FROM Ratings rt
        JOIN Drivers d ON rt.rated_user_id=d.user_id
        JOIN Users u ON d.user_id=u.user_id
        WHERE rt.rated_by='Rider'
        GROUP BY d.driver_id, u.full_name, d.city
        HAVING AVG(rt.score) < 3.5
        ORDER BY avg_rating ASC
    """))

@app.route('/api/reports/completed-trips-per-driver', methods=['GET'])
def completed_trips_per_driver():
    return jsonify(query("""
        SELECT d.driver_id, u.full_name, COUNT(h.ride_id) as completed_trips
        FROM Drivers d
        JOIN Users u ON d.user_id=u.user_id
        LEFT JOIN RideHistory h ON h.driver_id=d.driver_id AND h.final_status='Completed'
        GROUP BY d.driver_id, u.full_name
        ORDER BY completed_trips DESC
    """))

@app.route('/api/reports/trip-report', methods=['GET'])
def trip_report():
    return jsonify(query("""
        SELECT r.ride_id, r.ride_status, r.fare, r.created_at,
               ru.full_name as rider_name, ru.phone as rider_phone,
               du.full_name as driver_name, du.phone as driver_phone,
               v.make, v.model, v.license_plate, v.vehicle_type
        FROM Rides r
        JOIN Users ru ON r.rider_id=ru.user_id
        JOIN Drivers d ON r.driver_id=d.driver_id
        JOIN Users du ON d.user_id=du.user_id
        JOIN Vehicles v ON r.vehicle_id=v.vehicle_id
        ORDER BY r.created_at DESC
        LIMIT 100
    """))

@app.route('/api/reports/riders-completion', methods=['GET'])
def riders_completion_report():
    return jsonify(query("""
        SELECT u.user_id, u.full_name, u.email,
               COUNT(h.ride_id) as completed_rides,
               MAX(h.archived_at) as last_completed_at
        FROM Users u
        LEFT JOIN RideHistory h
          ON h.rider_id=u.user_id AND h.final_status='Completed'
        WHERE u.role='rider'
        GROUP BY u.user_id, u.full_name, u.email
        ORDER BY completed_rides DESC, u.full_name
    """))

@app.route('/api/reports/promo-usage', methods=['GET'])
def promo_usage_report():
    return jsonify(query("""
        SELECT r.ride_id, r.promo_code, p.amount, p.promo_discount, p.payment_status, p.transaction_date,
               pc.discount_percent, pc.usage_count
        FROM Rides r
        JOIN Payments p ON p.ride_id=r.ride_id
        LEFT JOIN PromoCodes pc ON r.promo_code=pc.code
        ORDER BY p.transaction_date DESC
        LIMIT 100
    """))

@app.route('/api/reports/revenue', methods=['GET'])
def revenue_report():
    return jsonify(query("""
        SELECT DATE(p.transaction_date) as date,
               SUM(p.amount) as total_revenue,
               p.payment_method,
               COUNT(*) as transaction_count
        FROM Payments p WHERE p.payment_status='Paid'
        GROUP BY DATE(p.transaction_date), p.payment_method
        ORDER BY date DESC LIMIT 30
    """))

@app.route('/api/reports/driver-earnings', methods=['GET'])
def driver_earnings():
    return jsonify(query("""
        SELECT u.full_name, d.wallet_balance, d.total_trips, d.average_rating, d.city
        FROM Drivers d JOIN Users u ON d.user_id=u.user_id
        ORDER BY d.wallet_balance DESC
    """))

@app.route('/api/reports/refunds', methods=['GET'])
def refund_report():
    return jsonify(query("""
        SELECT COUNT(*) as refund_count, SUM(amount) as total_refunded
        FROM Payments WHERE payment_status='Refunded'
    """))

# ─── VIEWS ──────────────────────────────────────────────────────────────────
@app.route('/api/views/active-rides', methods=['GET'])
def active_rides_view():
    return jsonify(query("SELECT * FROM ActiveRidesView ORDER BY created_at DESC LIMIT 100"))

@app.route('/api/views/top-drivers', methods=['GET'])
def top_drivers_view():
    return jsonify(query("SELECT * FROM TopDriversView ORDER BY average_rating DESC LIMIT 50"))

# ─── LEADERBOARD VIEW ────────────────────────────────────────────────────────
@app.route('/api/leaderboard', methods=['GET'])
def leaderboard():
    return jsonify(query("SELECT * FROM DriverLeaderboard LIMIT 20"))

# ─── ADMIN FLAGS ─────────────────────────────────────────────────────────────
@app.route('/api/flags', methods=['GET'])
def get_flags():
    return jsonify(query("""
        SELECT f.*, u.full_name, u.role FROM AdminFlags f
        JOIN Users u ON f.user_id=u.user_id
        ORDER BY f.flagged_at DESC
    """))

# ─── SERVE HTML ──────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/index.html')
def index_html():
    return send_from_directory('.', 'index.html')

@app.route('/admin')
@app.route('/admin.html')
def admin_page():
    return send_from_directory('.', 'admin.html')

@app.route('/rider')
@app.route('/rider.html')
def rider_page():
    return send_from_directory('.', 'rider.html')

@app.route('/driver')
@app.route('/driver.html')
def driver_page():
    return send_from_directory('.', 'driver.html')

if __name__ == '__main__':
    with app.app_context():
        setup()
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=5000, use_reloader=False)