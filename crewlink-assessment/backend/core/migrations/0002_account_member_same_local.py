"""
Postgres-level guarantee that an Account never points at another local's member.

Account.clean() only runs through .save(); bulk_create, .update() and raw SQL skip it.
A composite FK closes that gap: (member_id, local_id) must match a real member's
(id, local_id). When member_id is NULL (a leader), Postgres skips the check.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="member",
            constraint=models.UniqueConstraint(fields=("id", "local"), name="member_id_local_uniq"),
        ),
        migrations.RunSQL(
            sql="""
                ALTER TABLE core_account
                  ADD CONSTRAINT account_member_same_local
                  FOREIGN KEY (member_id, local_id) REFERENCES core_member (id, local_id);
            """,
            reverse_sql="ALTER TABLE core_account DROP CONSTRAINT account_member_same_local;",
        ),
    ]
