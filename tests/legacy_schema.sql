-- NextStep database schema (Postgres). Current full schema, including the
-- columns/tables added for the web app (marked "added"). seed.py applies the
-- additions idempotently to the existing database and loads the page data.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE users (
  id               uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  full_name        varchar NOT NULL,
  email            varchar NOT NULL UNIQUE,
  password_hash    varchar NOT NULL,               -- bcrypt
  role             varchar NOT NULL CHECK (role IN ('student', 'mentor', 'tutor', 'admin')),
  unweighted_gpa   numeric CHECK (unweighted_gpa >= 0.0 AND unweighted_gpa <= 4.0),
  annual_budget    integer CHECK (annual_budget >= 0),
  points_balance   integer DEFAULT 200 CHECK (points_balance >= 0),
  created_at       timestamptz DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE checklist_items (
  id            serial PRIMARY KEY,
  title         varchar NOT NULL,
  tag           varchar NOT NULL CHECK (tag IN ('FAFSA', 'TAP', 'SAT/ACT')),
  point_reward  integer DEFAULT 100
);

CREATE TABLE user_checklist_progress (
  id                 uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id            uuid REFERENCES users(id) ON DELETE CASCADE,
  checklist_item_id  integer REFERENCES checklist_items(id) ON DELETE CASCADE,
  is_completed       boolean DEFAULT false,
  completed_at       timestamptz,
  UNIQUE (user_id, checklist_item_id)
);

CREATE TABLE scholarships (
  id                  uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  title               varchar NOT NULL,
  provider            varchar NOT NULL,
  amount              integer NOT NULL CHECK (amount > 0),
  deadline            date NOT NULL,
  min_gpa             numeric DEFAULT 0.0,
  description         text NOT NULL,
  is_custom           boolean DEFAULT false,      -- true = added by a student, shown only to them
  created_by_user_id  uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at          timestamptz DEFAULT CURRENT_TIMESTAMP,
  slug                varchar UNIQUE,             -- added: page id, e.g. 'sch-1' (NULL for custom ones)
  major               varchar                     -- added
);

CREATE TABLE user_saved_scholarships (
  user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  scholarship_id  uuid NOT NULL REFERENCES scholarships(id) ON DELETE CASCADE,
  saved_at        timestamptz DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id, scholarship_id)
);

CREATE TABLE mentors_and_tutors (
  id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id       uuid REFERENCES users(id) ON DELETE CASCADE,   -- NULL if the person has no login
  university    varchar NOT NULL,
  major         varchar NOT NULL,
  test_scores   varchar,
  bio           text NOT NULL,
  avatar_url    text,
  status        varchar DEFAULT 'available' CHECK (status IN ('available', 'busy', 'pending_verification')),
  created_at    timestamptz DEFAULT CURRENT_TIMESTAMP,
  slug          varchar UNIQUE,                                -- added: page id, e.g. 'tut-1', 'men-1'
  kind          varchar NOT NULL DEFAULT 'mentor'
                CONSTRAINT mentors_and_tutors_kind_check CHECK (kind IN ('mentor', 'tutor')),  -- added
  display_name  varchar,                                       -- added
  rate          varchar,                                       -- added
  specialties   text[] NOT NULL DEFAULT '{}'                   -- added
);

CREATE TABLE mentor_requests (
  id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  student_id    uuid REFERENCES users(id) ON DELETE CASCADE,
  mentor_id     uuid REFERENCES mentors_and_tutors(id) ON DELETE CASCADE,
  request_type  varchar NOT NULL CHECK (request_type IN ('mentorship', 'act_sat_tutoring')),
  notes         text,
  status        varchar DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'completed', 'declined')),
  requested_at  timestamptz DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE rewards_shop (
  id              uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  title           varchar NOT NULL,
  cost_points     integer NOT NULL CHECK (cost_points > 0),
  category        varchar NOT NULL CHECK (category IN ('Gift Card', 'Merch')),
  icon_name       varchar DEFAULT 'gift',         -- Lucide icon name
  description     text NOT NULL,
  stock_quantity  integer DEFAULT 100,
  slug            varchar UNIQUE                  -- added: page id, e.g. 'rew-1'
);

CREATE TABLE redemptions (
  id                         uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id                    uuid REFERENCES users(id) ON DELETE CASCADE,
  reward_id                  uuid REFERENCES rewards_shop(id) ON DELETE CASCADE,
  shipping_address_or_email  varchar NOT NULL,
  size_or_option             varchar,
  status                     varchar DEFAULT 'processing' CHECK (status IN ('processing', 'digital_code_sent', 'shipped')),
  redeemed_at                timestamptz DEFAULT CURRENT_TIMESTAMP
);

-- added: College Matcher tab
CREATE TABLE colleges (
  id              uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  slug            varchar UNIQUE NOT NULL,        -- e.g. 'col-1'
  name            varchar NOT NULL,
  type            varchar NOT NULL CHECK (type IN ('Public', 'Private')),
  location        varchar NOT NULL,
  min_gpa         numeric NOT NULL DEFAULT 0.0,
  annual_tuition  integer NOT NULL CHECK (annual_tuition >= 0),
  accept_rate     varchar,
  sat_range       varchar,
  image_url       text,
  majors          text[] NOT NULL DEFAULT '{}',
  description     text NOT NULL,
  created_at      timestamptz DEFAULT CURRENT_TIMESTAMP
);

-- added: Internships tab
CREATE TABLE internships (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  slug         varchar UNIQUE NOT NULL,           -- e.g. 'int-1'
  title        varchar NOT NULL,
  org          varchar NOT NULL,
  target       varchar NOT NULL CHECK (target IN ('High School', 'College')),
  pay          varchar,
  description  text NOT NULL,
  created_at   timestamptz DEFAULT CURRENT_TIMESTAMP
);
