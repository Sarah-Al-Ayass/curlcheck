# Curl Check

A Streamlit app: create an account, take a 1-minute hair quiz, get a personal routine
(3 steps, 2–3 products, 1 habit to stop), add optional details for extra tips, and leave feedback.
The team dashboard shows Traction (accounts per day) and Impact (usefulness, time saved).

## Put it online (about 20 minutes)

### 1. Database (Supabase, free)
1. Go to supabase.com, sign in, click **New project**.
2. Open **SQL Editor → New query**, paste everything in `schema.sql`, click **Run**.
3. Open **Project Settings → API**. Copy the **Project URL** and the **service_role** key (keep this key secret).

### 2. Code (GitHub)
1. Create a new repository on github.com, for example `curlcheck`.
2. Upload these files: `app.py`, `requirements.txt`, `schema.sql`, `README.md`, `.gitignore`, and the `.streamlit/config.toml` file.
   Do NOT upload a `secrets.toml`.

### 3. App (Streamlit Community Cloud, free)
1. Go to share.streamlit.io, sign in with GitHub, click **Create app**.
2. Pick your repository, branch `main`, file `app.py`.
3. Open **Advanced settings → Secrets** and paste (with your real values):
   ```
   SUPABASE_URL = "https://YOUR-PROJECT.supabase.co"
   SUPABASE_KEY = "YOUR-SERVICE-ROLE-KEY"
   ADMIN_PASSWORD = "choose-a-team-password"
   ```
4. Click **Deploy**. You get a link like `https://curlcheck.streamlit.app` to send to people.

### Team dashboard
Open the sidebar (arrow top left), type the ADMIN_PASSWORD. You see accounts, routines, feedback and charts.

## Run on your computer (optional)
```
pip install -r requirements.txt
streamlit run app.py
```
Without Supabase secrets the app runs in test mode and saves accounts in a local file.

## Privacy
Passwords are stored hashed (PBKDF2). The medication question stores only yes / no / prefer not to say.
Delete a member on request in Supabase: Table Editor → members → delete the row.
