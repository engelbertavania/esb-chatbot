"""Try common Supabase pooler hosts to find which one accepts the project ref."""
import psycopg2

project = "midahxroauieyzaiiuvf"
password = "sukatampil123"

regions = [
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-northeast-1",
    "ap-south-1",
    "us-east-1",
    "us-west-1",
    "eu-west-1",
    "eu-central-1",
]
shards = ["aws-0", "aws-1"]
hosts: list[str] = []
for shard in shards:
    for region in regions:
        hosts.append(f"{shard}-{region}.pooler.supabase.com")

# Try with project ref in username (modern Supavisor format)
for host in hosts:
    dsn = (
        f"host={host} port=5432 user=postgres.{project} password={password} "
        f"dbname=postgres connect_timeout=5"
    )
    try:
        conn = psycopg2.connect(dsn)
        conn.close()
        print(f"\n*** FOUND ***")
        print(f"Host: {host}")
        print(f"URL:  postgresql://postgres.{project}:{password}@{host}:5432/postgres")
        break
    except psycopg2.OperationalError as e:
        msg = str(e).strip().split("\n")[0]
        # Trim noise
        short = msg.split("FATAL: ")[-1] if "FATAL" in msg else msg[:80]
        print(f"  {host}: {short}")
