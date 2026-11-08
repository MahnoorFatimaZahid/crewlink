"""
Postgres-level guarantee that a recipient row never crosses locals.

The row's local_id is copied in for scoping, so it must agree with both its
announcement's local and its member's local. Two composite FKs enforce that.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("announcements", "0001_initial"),
        ("core", "0002_account_member_same_local"),  # creates member (id, local_id) unique
    ]

    operations = [
        migrations.AddConstraint(
            model_name="announcement",
            constraint=models.UniqueConstraint(fields=("id", "local"),
                                               name="announcement_id_local_uniq"),
        ),
        migrations.RunSQL(
            sql="""
                ALTER TABLE announcements_announcementrecipient
                  ADD CONSTRAINT recipient_announcement_same_local
                  FOREIGN KEY (announcement_id, local_id)
                  REFERENCES announcements_announcement (id, local_id) ON DELETE CASCADE;
                ALTER TABLE announcements_announcementrecipient
                  ADD CONSTRAINT recipient_member_same_local
                  FOREIGN KEY (member_id, local_id) REFERENCES core_member (id, local_id);
            """,
            reverse_sql="""
                ALTER TABLE announcements_announcementrecipient
                  DROP CONSTRAINT recipient_announcement_same_local;
                ALTER TABLE announcements_announcementrecipient
                  DROP CONSTRAINT recipient_member_same_local;
            """,
        ),
    ]
