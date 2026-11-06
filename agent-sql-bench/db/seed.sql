-- Tiny deterministic "shop" database. Runs once when the container's data dir is empty.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE customers (
    id      integer PRIMARY KEY,
    name    text NOT NULL,
    region  text NOT NULL,
    joined  date NOT NULL
);

CREATE TABLE products (
    id        integer PRIMARY KEY,
    name      text NOT NULL,
    category  text NOT NULL,
    price     numeric(10,2) NOT NULL
);

CREATE TABLE orders (
    id           integer PRIMARY KEY,
    customer_id  integer NOT NULL REFERENCES customers(id),
    ordered_at   date NOT NULL,
    status       text NOT NULL
);

CREATE TABLE order_items (
    order_id    integer NOT NULL REFERENCES orders(id),
    product_id  integer NOT NULL REFERENCES products(id),
    quantity    integer NOT NULL,
    PRIMARY KEY (order_id, product_id)
);

-- Deterministic data: values derived from ids, no random().
INSERT INTO customers
SELECT i,
       'customer_' || i,
       (ARRAY['north','south','east','west'])[1 + i % 4],
       DATE '2023-01-01' + (i * 7 % 700)
FROM generate_series(1, 500) AS i;

INSERT INTO products
SELECT i,
       'product_' || i,
       (ARRAY['books','games','tools','food','toys'])[1 + i % 5],
       (5 + (i * 37 % 200))::numeric(10,2)
FROM generate_series(1, 100) AS i;

INSERT INTO orders
SELECT i,
       1 + (i * 13 % 500),
       DATE '2024-01-01' + (i * 11 % 365),
       (ARRAY['placed','shipped','delivered','returned'])[1 + i % 4]
FROM generate_series(1, 5000) AS i;

INSERT INTO order_items
SELECT o, 1 + ((o * 7 + k * 31) % 100), 1 + ((o + k) % 5)
FROM generate_series(1, 5000) AS o, generate_series(0, 2) AS k;

CREATE INDEX ON orders (customer_id);
CREATE INDEX ON order_items (product_id);
ANALYZE;
