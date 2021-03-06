"""
Wrap up PostgreSQL and PostGIS into a convenient class.

Examples
--------

Create a database and import a shapefile:

    >>> import postgis_helpers as pGIS
    >>> db = pGIS.PostgreSQL("my_database_name")
    >>> db.create()
    >>> db.import_geodata("bike_lanes", "http://url.to.shapefile")
    >>> bike_gdf = db.query_as_geo_df("select * from bike_lanes")

"""
import os
import subprocess
import pandas as pd
import geopandas as gpd

import psycopg2
import sqlalchemy
from geoalchemy2 import Geometry, WKTElement

from typing import Union
from pathlib import Path

from .sql_helpers import sql_hex_grid_function_definition
from .general_helpers import now, report_time_delta, dt_as_time
from .geopandas_helpers import spatialize_point_dataframe
from .console import _console, RichStyle, RichSyntax
from .config_helpers import DEFAULT_DATA_INBOX, DEFAULT_DATA_OUTBOX


class PostgreSQL:
    """
    This class encapsulates interactions with a ``PostgreSQL``
    database. It leverages ``psycopg2``, ``sqlalchemy``, and ``geoalchemy2``
    as needed. It stores connection information that includes:
        - database name
        - username & password
        - host & port
        - superusername & password
        - the SQL cluster's master database
        - ``verbosity`` level, which controls how much gets printed out
    """

    def __init__(
        self,
        working_db: str,
        un: str = "postgres",
        pw: str = "password1",
        host: str = "localhost",
        port: int = 5432,
        sslmode: str = None,
        super_db: str = "postgres",
        super_un: str = "postgres",
        super_pw: str = "password2",
        active_schema: str = "public",
        verbosity: str = "full",
        data_inbox: Path = DEFAULT_DATA_INBOX,
        data_outbox: Path = DEFAULT_DATA_OUTBOX,
    ):
        """
        Initialize a database object with placeholder values.

        :param working_db: Name of the database you want to connect to
        :type working_db: str
        :param un: User name within the database, defaults to "postgres"
        :type un: str, optional
        :param pw: Password for the user, defaults to "password1"
        :type pw: str, optional
        :param host: Host where the database lives, defaults to "localhost"
        :type host: str, optional
        :param port: Port number on the host, defaults to 5432
        :type port: int, optional
        :param sslmode: False or string like "require", defaults to False
        :type sslmode: Union[bool, str], optional
        :param super_db: SQL cluster root db, defaults to "postgres"
        :type super_db: str, optional
        :param super_un: SQL cluster root user, defaults to "postgres"
        :type super_un: str, optional
        :param super_pw: SQL cluster root password, defaults to "password2"
        :type super_pw: str, optional
        :param verbosity: Control how much gets printed out to the console,
                          defaults to ``"full"``. Other options include
                          ``"minimal"`` and ``"errors"``
        :type verbosity: str, optional

        TODO: add data box, print style, schema params
        """

        self.DATABASE = working_db
        self.USER = un
        self.PASSWORD = pw
        self.HOST = host
        self.PORT = port
        self.SSLMODE = sslmode
        self.SUPER_DB = super_db
        self.SUPER_USER = super_un
        self.SUPER_PASSWORD = super_pw
        self.ACTIVE_SCHEMA = active_schema

        for folder in [data_inbox, data_outbox]:
            if not folder.exists():
                folder.mkdir(parents=True)

        self.DATA_INBOX = data_inbox
        self.DATA_OUTBOX = data_outbox

        verbosity_options = ["full", "minimal", "errors"]

        if verbosity in verbosity_options:
            self.VERBOSITY = verbosity
        else:
            msg = f"verbosity must be one of: {verbosity_options}"
            raise ValueError(msg)

        if not self.exists():
            self.db_create()

        msg = f":person_surfing::water_wave: {self.DATABASE} @ {self.HOST} :water_wave::water_wave:"
        self._print(3, msg)

    def connection_details(self) -> dict:
        """
        Return a dictionary that can be used to
        instantiate other database connections on the
        same SQL cluster.

        :return: Dictionary with all of the SQL cluster connection info
        :rtype: dict
        """
        details = {
            "un": self.USER,
            "pw": self.PASSWORD,
            "host": self.HOST,
            "port": self.PORT,
            "sslmode": self.SSLMODE,
            "super_db": self.SUPER_DB,
            "super_un": self.SUPER_USER,
            "super_pw": self.SUPER_PASSWORD,
        }

        return details

    def _print(self, level: int, message: str):
        """
        Messages will print out depending on the VERBOSITY property
        and the importance level provided.

        VERBOSITY options include: ``full``, ``minimal``, and ``errors``

            1 = Only prints in ``full``
            2 = Prints in ``full`` and ``minimal``,
                but does not print in ``errors``
            3 = Always prints out


        :param level: [description]
        :type level: int
        :param message: [description]
        :type message: str
        """

        print_out = False

        if level == 1:
            prefix = "\t"
            style = RichStyle()

        elif level == 2:
            prefix = ":backhand_index_pointing_right: "
            style = RichStyle()

        elif level == 3:
            prefix = ""
            # style.color = "blue"
            style = RichStyle(color="green4", bold=True)

        if self.VERBOSITY == "full" and level in [1, 2, 3]:
            print_out = True

        elif self.VERBOSITY == "minimal" and level in [2, 3]:
            print_out = True

        elif self.VERBOSITY == "errors" and level in [3]:
            print_out = True

        if print_out:
            if type(message) == str:
                msg = prefix + message
                _console.print(msg, style=style)
            elif type(message) == RichSyntax:
                _console.print(message)
            else:
                _console.print(f"Type error: {type(message)}")

    def timer(func):
        """
        Decorator function that will record &
        report on how long it takes for another
        function to execute.

        :param func: the function to be timed
        :type func: function
        """

        def magic(self, *args, **kwargs):
            start_time = now()

            msg = f":hourglass_not_done: starting @ {dt_as_time(start_time)}"
            self._print(1, msg)

            function_return_value = func(self, *args, **kwargs)

            end_time = now()

            # Print runtime out when "full"
            msg = f":hourglass_done: finished @ {dt_as_time(end_time)}"
            self._print(1, msg)

            runtime_msg = report_time_delta(start_time, end_time)
            self._print(1, runtime_msg)

            return function_return_value

        return magic

    def add_schema(self, schema: str) -> None:
        """
        Add a schema if it does not yet exist.

        :param schema: any valid name for a SQL schema
        :type query: str
        """
        self.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    # QUERY the database
    # ------------------

    def query_as_list(self, query: str, super_uri: bool = False) -> list:
        """
        Query the database and get the result as a ``list``

        :param query: any valid SQL query string
        :type query: str
        :param super_uri: flag that will execute against the
                          super db/user, defaults to False
        :type super_uri: bool, optional
        :return: list with each item being a row from the query result
        :rtype: list
        """
        self._print(1, "... querying ...")
        code_w_highlight = RichSyntax(query, "sql", theme="monokai", line_numbers=True)
        self._print(1, code_w_highlight)

        uri = self.uri(super_uri=super_uri)

        connection = psycopg2.connect(uri)

        cursor = connection.cursor()

        cursor.execute(query)

        result = cursor.fetchall()

        cursor.close()
        connection.close()

        return result

    def query_as_df(self, query: str, super_uri: bool = False) -> pd.DataFrame:
        """
        Query the database and get the result as a ``pandas.DataFrame``

        :param query: any valid SQL query string
        :type query: str
        :param super_uri: flag that will execute against the
                          super db/user, defaults to False
        :type super_uri: bool, optional
        :return: dataframe with the query result
        :rtype: pd.DataFrame
        """

        self._print(1, "... querying ...")
        code_w_highlight = RichSyntax(query, "sql", theme="monokai", line_numbers=True)
        self._print(1, code_w_highlight)

        uri = self.uri(super_uri=super_uri)

        engine = sqlalchemy.create_engine(uri)
        df = pd.read_sql(query, engine)
        engine.dispose()

        return df

    def query_as_geo_df(self, query: str, geom_col: str = "geom") -> gpd.GeoDataFrame:
        """
        Query the database and get the result as a ``geopandas.GeoDataFrame``

        :param query: any valid SQL query string
        :type query: str
        :param geom_col: name of the column that holds the geometry,
                         defaults to 'geom'
        :type geom_col: str
        :return: geodataframe with the query result
        :rtype: gpd.GeoDataFrame
        """
        self._print(1, "... querying ...")
        code_w_highlight = RichSyntax(query, "sql", theme="monokai", line_numbers=True)
        self._print(1, code_w_highlight)

        connection = psycopg2.connect(self.uri())

        gdf = gpd.GeoDataFrame.from_postgis(query, connection, geom_col=geom_col)

        connection.close()

        return gdf

    def query_as_single_item(self, query: str, super_uri: bool = False):
        """
        Query the database and get the result as a SINGLETON.
        For when you want to transform ``[(True,)]`` into ``True``

        :param query: any valid SQL query string
        :type query: str
        :param super_uri: flag that will execute against the
                          super db/user, defaults to False
        :type super_uri: bool, optional
        :return: result from the query
        :rtype: singleton
        """
        self._print(1, "... querying ...")
        code_w_highlight = RichSyntax(query, "sql", theme="monokai", line_numbers=True)
        self._print(1, code_w_highlight)

        result = self.query_as_list(query, super_uri=super_uri)

        return result[0][0]

    # EXECUTE queries to make them persistent
    # ---------------------------------------

    def execute(self, query: str, autocommit: bool = False):
        """
        Execute a query for a persistent result in the database.
        Use ``autocommit=True`` when creating and deleting databases.

        :param query: any valid SQL query string
        :type query: str
        :param autocommit: flag that will execute against the
                           super db/user, defaults to False
        :type autocommit: bool, optional
        """

        self._print(1, "... executing ...")

        if len(query) < 5000:
            code_w_highlight = RichSyntax(query, "sql", theme="monokai", line_numbers=True)
            self._print(1, code_w_highlight)

        uri = self.uri(super_uri=autocommit)

        connection = psycopg2.connect(uri)
        if autocommit:
            connection.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)

        cursor = connection.cursor()

        cursor.execute(query)

        cursor.close()
        connection.commit()
        connection.close()

    # DATABASE-level helper functions
    # -------------------------------

    def uri(self, super_uri: bool = False) -> str:
        """
        Create a connection string URI for this database.

        :param super_uri: Flag that will provide access to cluster
                          root if True, defaults to False
        :type super_uri: bool, optional
        :return: Connection string URI for PostgreSQL
        :rtype: str
        """

        # If super_uri is True, use the super un/pw/db
        if super_uri:
            user = self.SUPER_USER
            pw = self.SUPER_PASSWORD
            database = self.SUPER_DB

        # Otherwise, use the normal connection info
        else:
            user = self.USER
            pw = self.PASSWORD
            database = self.DATABASE

        connection_string = f"postgresql://{user}:{pw}@{self.HOST}:{self.PORT}/{database}"

        if self.SSLMODE:
            connection_string += f"?sslmode={self.SSLMODE}"

        return connection_string

    def exists(self) -> bool:
        """
        Does this database exist yet? Returns True or False

        :return: True or False if the database exists on the cluster
        :rtype: bool
        """

        sql_db_exists = f"""
            SELECT EXISTS(
                SELECT datname FROM pg_catalog.pg_database
                WHERE lower(datname) = lower('{self.DATABASE}')
            );
        """
        return self.query_as_single_item(sql_db_exists, super_uri=True)

    def db_create(self) -> None:
        """
        Create this database if it doesn't exist yet
        """

        if self.exists():
            self._print(1, f"Database {self.DATABASE} already exists")
        else:
            self._print(3, f"Creating database: {self.DATABASE} on {self.HOST}")

            sql_make_db = f"CREATE DATABASE {self.DATABASE};"

            self.execute(sql_make_db, autocommit=True)

            # Add PostGIS if not already installed
            if "geometry_columns" in self.all_tables_as_list():
                self._print(1, "PostGIS comes pre-installed")
            else:
                self._print(1, "Installing PostGIS")

                sql_add_postgis = "CREATE EXTENSION postgis;"
                self.execute(sql_add_postgis)

            # Load the custom Hexagon Grid function
            self._print(1, "Installing custom hexagon grid function")
            self.execute(sql_hex_grid_function_definition)

    def db_delete(self) -> None:
        """Delete this database (if it exists)"""

        if not self.exists():
            self._print(1, "This database does not exist, nothing to delete!")
        else:
            self._print(3, f"Deleting database: {self.DATABASE} on {self.HOST}")
            sql_drop_db = f"DROP DATABASE {self.DATABASE};"
            self.execute(sql_drop_db, autocommit=True)

    @timer
    def db_export_pgdump_file(self, output_folder: Path = None) -> Path:
        """
        Save this database to a ``.sql`` file.
        Requires ``pg_dump`` to be accessible via the command line.


        :param output_folder: Folder path to write .sql file to
        :type output_folder: pathlib.Path
        :return: Filepath to SQL file that was created
        :rtype: str
        """

        if not output_folder:
            output_folder = self.DATA_OUTBOX

        # Get a string for today's date and time,
        # like '2020_06_10' and '14_13_38'
        rightnow = str(now())
        today = rightnow.split(" ")[0].replace("-", "_")
        timestamp = rightnow.split(" ")[1].replace(":", "_").split(".")[0]

        # Use pg_dump to save the database to disk
        sql_name = f"{self.DATABASE}_d_{today}_t_{timestamp}.sql"
        sql_file = output_folder / sql_name

        self._print(2, f"Exporting {self.DATABASE} to {sql_file}")

        system_call = f'pg_dump {self.uri()} > "{sql_file}" '
        os.system(system_call)

        return sql_file

    @timer
    def db_load_pgdump_file(self, sql_dump_filepath: Path, overwrite: bool = True) -> None:
        """
        Populate the database by loading from a SQL file that
        was previously created by ``pg_dump``.

        :param sql_dump_filepath: filepath to the ``.sql`` dump file
        :type sql_dump_filepath: Union[Path, str]
        :param overwrite: flag that controls whether or not this
                          function will replace the existing database
        :type overwrite: bool
        """

        if self.exists():
            if overwrite:
                self.db_delete()
                self.db_create()
            else:
                self._print(
                    3,
                    f"Database named {self.DATABASE} already exists and overwrite=False!",
                )
                return

        self._print(2, f"Loading {self.DATABASE} from {sql_dump_filepath}")

        system_command = f'psql "{self.uri()}" <  "{sql_dump_filepath}"'
        os.system(system_command)

    # LISTS of things inside this database (or the cluster at large)
    # --------------------------------------------------------------

    def all_tables_as_list(self, schema: str = None) -> list:
        """
        Get a list of all tables in the database.
        Optionally filter to a schema

        :param schema: name of the schema to filter by
        :type schema: str
        :return: List of tables in the database
        :rtype: list
        """

        sql_all_tables = """
            SELECT table_name
            FROM information_schema.tables
        """

        if schema:
            sql_all_tables += f"""
                WHERE table_schema = '{schema}'
        """

        tables = self.query_as_list(sql_all_tables)

        return [t[0] for t in tables]

    def all_spatial_tables_as_dict(self, schema: str = None) -> dict:
        """
        Get a dictionary of all spatial tables in the database.
        Return value is formatted as: ``{table_name: epsg}``

        :return: Dictionary with spatial table names as keys
                 and EPSG codes as values.
        :rtype: dict
        """

        sql_all_spatial_tables = """
            SELECT f_table_name AS tblname, srid
            FROM geometry_columns
        """

        if schema:
            sql_all_spatial_tables += f"""
                WHERE f_table_schema = '{schema}'
        """

        spatial_tables = self.query_as_list(sql_all_spatial_tables)

        return {t[0]: t[1] for t in spatial_tables}

    def all_databases_on_cluster_as_list(self) -> list:
        """
        Get a list of all databases on this SQL cluster.

        :return: List of all databases on the cluster
        :rtype: list
        """

        sql_all_databases = f"""
            SELECT datname FROM pg_database
            WHERE datistemplate = false
                AND datname != '{self.SUPER_DB}'
                AND LEFT(datname, 1) != '_';
        """

        database_list = self.query_as_list(sql_all_databases, super_uri=True)

        return [d[0] for d in database_list]

    # TABLE-level helper functions
    # ----------------------------

    def table_columns_as_list(self, table_name: str, schema: str = None) -> list:
        """
        Get a list of all columns in a table.

        :param table_name: Name of the table
        :type table_name: str
        :return: List of all columns in a table
        :rtype: list
        """

        if not schema:
            schema = self.ACTIVE_SCHEMA

        sql_all_cols_in_table = f"""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = '{schema}'
                AND table_name = '{table_name}';
        """

        column_list = self.query_as_list(sql_all_cols_in_table)

        column_names = [c[0] for c in column_list]

        return column_names

    def table_add_or_nullify_column(
        self, table_name: str, column_name: str, column_type: str, schema: str = None
    ) -> None:
        """
        Add a new column to a table.
        Overwrite to ``NULL`` if it already exists.

        :param table_name: Name of the table
        :type table_name: str
        :param column_name: Name of the new column
        :type column_name: str
        :param column_type: Data type of the column. Must be valid in PgSQL
        :type column_type: str
        """

        if not schema:
            schema = self.ACTIVE_SCHEMA

        msg = f"Adding {column_type} col named {column_name} to {schema}.{table_name}"
        self._print(1, msg)

        existing_columns = self.table_columns_as_list(table_name, schema=schema)

        if column_name in existing_columns:
            query = f"""
                UPDATE {schema}.{table_name} SET {column_name} = NULL;
            """
        else:
            query = f"""
                ALTER TABLE {schema}.{table_name}
                ADD COLUMN {column_name} {column_type};
            """

        self.execute(query)

    def table_add_uid_column(
        self, table_name: str, schema: str = None, uid_col: str = "uid"
    ) -> None:
        """
        Add a serial primary key column named 'uid' to the table.

        :param table_name: Name of the table to add a uid column to
        :type table_name: str
        """

        if not schema:
            schema = self.ACTIVE_SCHEMA

        self._print(1, f"Adding uid column to {schema}.{table_name}")

        sql_unique_id_column = f"""
            ALTER TABLE {schema}.{table_name} DROP COLUMN IF EXISTS {uid_col};
            ALTER TABLE {schema}.{table_name} ADD {uid_col} serial PRIMARY KEY;
        """
        self.execute(sql_unique_id_column)

    def table_add_spatial_index(self, table_name: str, schema: str = None) -> None:
        """
        Add a spatial index to the 'geom' column in the table.

        :param table_name: Name of the table to make the index on
        :type table_name: str
        """

        if not schema:
            schema = self.ACTIVE_SCHEMA

        self._print(1, f"Creating a spatial index on {schema}.{table_name}")

        sql_make_spatial_index = f"""
            CREATE INDEX ON {schema}.{table_name}
            USING GIST (geom);
        """
        self.execute(sql_make_spatial_index)

    def table_reproject_spatial_data(
        self,
        table_name: str,
        old_epsg: Union[int, str],
        new_epsg: Union[int, str],
        geom_type: str,
        schema: str = None,
    ) -> None:
        """
        Transform spatial data from one EPSG into another EPSG.

        This can also be used with the same old and new EPSG. This
        is useful when making a new geotable, as this SQL code
        will update the table's entry in the ``geometry_columns`` table.

        :param table_name: name of the table
        :type table_name: str
        :param old_epsg: Current EPSG of the data
        :type old_epsg: Union[int, str]
        :param new_epsg: Desired new EPSG for the data
        :type new_epsg: Union[int, str]
        :param geom_type: PostGIS-valid name of the
                          geometry you're transforming
        :type geom_type: str
        """

        if not schema:
            schema = self.ACTIVE_SCHEMA

        msg = f"Reprojecting {schema}.{table_name} from {old_epsg} to {new_epsg}"
        self._print(1, msg)

        sql_transform_geom = f"""
            ALTER TABLE {schema}.{table_name}
            ALTER COLUMN geom TYPE geometry({geom_type}, {new_epsg})
            USING ST_Transform( ST_SetSRID( geom, {old_epsg} ), {new_epsg} );
        """
        self.execute(sql_transform_geom)

    def table_delete(self, table_name: str, schema: str = None) -> None:
        """
        Delete the table, cascade.

        :param table_name: Name of the table you want to delete.
        :type table_name: str
        """

        if not schema:
            schema = self.ACTIVE_SCHEMA

        self._print(2, f"Deleting table: {schema}.{table_name}")

        sql_drop_table = f"""
            DROP TABLE {schema}.{table_name} CASCADE;
        """
        self.execute(sql_drop_table)

    def table_spatialize_points(
        self,
        src_table: str,
        x_lon_col: str,
        y_lat_col: str,
        epsg: int,
        if_exists: str = "replace",
        new_table: str = None,
        schema: str = None,
    ) -> gpd.GeoDataFrame:

        if not schema:
            schema = self.ACTIVE_SCHEMA

        if not new_table:
            new_table = f"{src_table}_spatial"

        df = self.query_as_df(f"SELECT * FROM {schema}.{src_table};")

        gdf = spatialize_point_dataframe(df, x_lon_col=x_lon_col, y_lat_col=y_lat_col, epsg=epsg)

        self.import_geodataframe(gdf, new_table, if_exists=if_exists)

        self._print(2, f"Spatialized points from {src_table} into {new_table}")

    # IMPORT data into the database
    # -----------------------------

    def import_dataframe(
        self,
        dataframe: pd.DataFrame,
        table_name: str,
        if_exists: str = "fail",
        schema: str = None,
    ) -> None:
        """
        Import an in-memory ``pandas.DataFrame`` to the SQL database.

        Enforce clean column names (without spaces, caps, or weird symbols).

        :param dataframe: dataframe with data you want to save
        :type dataframe: pd.DataFrame
        :param table_name: name of the table that will get created
        :type table_name: str
        :param if_exists: pandas argument to handle overwriting data,
                          defaults to "fail"
        :type if_exists: str, optional
        """

        if not schema:
            schema = self.ACTIVE_SCHEMA

        self._print(2, f"Importing dataframe to: {schema}.{table_name}")

        # Replace "Column Name" with "column_name"
        dataframe.columns = dataframe.columns.str.replace(" ", "_")
        dataframe.columns = [x.lower() for x in dataframe.columns]

        # Remove '.' and '-' from column names.
        # i.e. 'geo.display-label' becomes 'geodisplaylabel'
        for s in [".", "-", "(", ")", "+"]:
            dataframe.columns = dataframe.columns.str.replace(s, "")

        # Write to database after making sure schema exists
        self.add_schema(schema)

        engine = sqlalchemy.create_engine(self.uri())
        dataframe.to_sql(table_name, engine, if_exists=if_exists, schema=schema)
        engine.dispose()

    def import_geodataframe(
        self,
        gdf: gpd.GeoDataFrame,
        table_name: str,
        src_epsg: Union[int, bool] = False,
        if_exists: str = "replace",
        schema: str = None,
        uid_col: str = "uid",
    ):
        """
        Import an in-memory ``geopandas.GeoDataFrame`` to the SQL database.

        :param gdf: geodataframe with data you want to save
        :type gdf: gpd.GeoDataFrame
        :param table_name: name of the table that will get created
        :type table_name: str
        :param src_epsg: The source EPSG code can be passed as an integer.
                         By default this function will try to read the EPSG
                         code directly, but some spatial data is funky and
                         requires that you explicitly declare its projection.
                         Defaults to False
        :type src_epsg: Union[int, bool], optional
        :param if_exists: pandas argument to handle overwriting data,
                          defaults to "replace"
        :type if_exists: str, optional
        """
        if not schema:
            schema = self.ACTIVE_SCHEMA

        # Read the geometry type. It's possible there are
        # both MULTIPOLYGONS and POLYGONS. This grabs the MULTI variant

        geom_types = list(gdf.geometry.geom_type.unique())
        geom_typ = max(geom_types, key=len).upper()

        self._print(2, f"Importing {geom_typ} geodataframe to: {schema}.{table_name}")

        # Manually set the EPSG if the user passes one
        if src_epsg:
            gdf.crs = f"epsg:{src_epsg}"
            epsg_code = src_epsg

        # Otherwise, try to get the EPSG value directly from the geodataframe
        else:
            # Older gdfs have CRS stored as a dict: {'init': 'epsg:4326'}
            if type(gdf.crs) == dict:
                epsg_code = int(gdf.crs["init"].split(" ")[0].split(":")[1])
            # Now geopandas has a different approach
            else:
                epsg_code = int(str(gdf.crs).split(":")[1])

        # Sanitize the columns before writing to the database
        # Make all column names lower case
        gdf.columns = [x.lower() for x in gdf.columns]

        # Replace the 'geom' column with 'geometry'
        if "geom" in gdf.columns:
            gdf["geometry"] = gdf["geom"]
            gdf.drop("geom", 1, inplace=True)

        # Drop the 'gid' column
        if "gid" in gdf.columns:
            gdf.drop("gid", 1, inplace=True)

        # Rename 'uid' to 'old_uid'
        if uid_col in gdf.columns:
            gdf[f"old_{uid_col}"] = gdf[uid_col]
            gdf.drop(uid_col, 1, inplace=True)

        # Build a 'geom' column using geoalchemy2
        # and drop the source 'geometry' column
        gdf["geom"] = gdf["geometry"].apply(lambda x: WKTElement(x.wkt, srid=epsg_code))
        gdf.drop("geometry", 1, inplace=True)

        # Write geodataframe to SQL database
        self.add_schema(schema)

        engine = sqlalchemy.create_engine(self.uri())
        gdf.to_sql(
            table_name,
            engine,
            if_exists=if_exists,
            index=True,
            index_label="gid",
            schema=schema,
            dtype={"geom": Geometry(geom_typ, srid=epsg_code)},
        )
        engine.dispose()

        self.table_add_uid_column(table_name, schema=schema, uid_col=uid_col)
        self.table_add_spatial_index(table_name, schema=schema)

    @timer
    def import_csv(
        self,
        table_name: str,
        csv_path: Path,
        if_exists: str = "append",
        schema: str = None,
        **csv_kwargs,
    ):
        r"""
        Load a CSV into a dataframe, then save the df to SQL.

        :param table_name: Name of the table you want to create
        :type table_name: str
        :param csv_path: Path to data. Anything accepted by Pandas works here.
        :type csv_path: Path
        :param if_exists: How to handle overwriting existing data,
                          defaults to ``"append"``
        :type if_exists: str, optional
        :param \**csv_kwargs: any kwargs for ``pd.read_csv()`` are valid here.
        """

        if not schema:
            schema = self.ACTIVE_SCHEMA

        self._print(2, "Loading CSV to dataframe")

        # Read the CSV with whatever kwargs were passed
        df = pd.read_csv(csv_path, **csv_kwargs)

        self.import_dataframe(df, table_name, if_exists=if_exists, schema=schema)

        return df

    def import_geodata(
        self,
        table_name: str,
        data_path: Path,
        src_epsg: Union[int, bool] = False,
        if_exists: str = "fail",
        schema: str = None,
    ):
        """
        Load geographic data into a geodataframe, then save to SQL.

        :param table_name: Name of the table you want to create
        :type table_name: str
        :param data_path: Path to the data. Anything accepted by Geopandas
                          works here.
        :type data_path: Path
        :param src_epsg: Manually declare the source EPSG if needed,
                         defaults to False
        :type src_epsg: Union[int, bool], optional
        :param if_exists: pandas argument to handle overwriting data,
                          defaults to "replace"
        :type if_exists: str, optional
        """

        if not schema:
            schema = self.ACTIVE_SCHEMA

        self._print(2, "Loading spatial data to geodataframe")

        # Read the data into a geodataframe
        gdf = gpd.read_file(data_path)

        # Drop null geometries
        gdf = gdf[gdf["geometry"].notnull()]

        # Explode multipart to singlepart and reset the index
        gdf = gdf.explode()
        gdf["explode"] = gdf.index
        gdf = gdf.reset_index()

        self.import_geodataframe(
            gdf, table_name, src_epsg=src_epsg, if_exists=if_exists, schema=schema
        )

    # CREATE data within the database
    # -------------------------------

    def make_geotable_from_query(
        self,
        query: str,
        new_table_name: str,
        geom_type: str,
        epsg: int,
        schema: str = None,
        uid_col: str = "uid",
    ) -> None:
        """
        TODO: docstring
        """

        if not schema:
            schema = self.ACTIVE_SCHEMA

        self._print(2, f"Making new geotable in DB : {new_table_name}")

        valid_geom_types = [
            "POINT",
            "MULTIPOINT",
            "POLYGON",
            "MULTIPOLYGON",
            "LINESTRING",
            "MULTILINESTRING",
        ]

        if geom_type.upper() not in valid_geom_types:
            for msg in [
                f"Geometry type of {geom_type} is not valid.",
                f"Please use one of the following: {valid_geom_types}",
                "Aborting",
            ]:
                self._print(3, msg)
            return

        sql_make_table_from_query = f"""
            DROP TABLE IF EXISTS {schema}.{new_table_name};
            CREATE TABLE {schema}.{new_table_name} AS
            {query}
        """

        self.add_schema(schema)

        self.execute(sql_make_table_from_query)

        self.table_add_uid_column(new_table_name, schema=schema, uid_col=uid_col)
        self.table_add_spatial_index(new_table_name, schema=schema)
        self.table_reproject_spatial_data(
            new_table_name, epsg, epsg, geom_type=geom_type.upper(), schema=schema
        )

    def make_hexagon_overlay(
        self,
        new_table_name: str,
        table_to_cover: str,
        desired_epsg: int,
        hexagon_size: float,
        schema: str = None,
    ) -> None:
        """
        Create a new spatial hexagon grid covering another
        spatial table. EPSG must be specified for the hexagons,
        as well as the size in square KM.

        :param new_table_name: Name of the new table to create
        :type new_table_name: str
        :param table_to_cover: Name of the existing table you want to cover
        :type table_to_cover: str
        :param desired_epsg: integer for EPSG you want the hexagons to be in
        :type desired_epsg: int
        :param hexagon_size: Size of the hexagons, 1 = 1 square KM
        :type hexagon_size: float
        """

        if not schema:
            schema = self.ACTIVE_SCHEMA

        self._print(2, f"Creating hexagon table named: {schema}.{new_table_name}")

        sql_create_hex_grid = f"""

            DROP TABLE IF EXISTS {schema}.{new_table_name};

            CREATE TABLE {schema}.{new_table_name} (
                gid SERIAL NOT NULL PRIMARY KEY,
                geom GEOMETRY('POLYGON', {desired_epsg}, 2) NOT NULL
            )
            WITH (OIDS=FALSE);

            INSERT INTO {schema}.{new_table_name} (geom)
            SELECT
                hex_grid(
                    {hexagon_size},
                    (select st_xmin(st_transform(st_collect(geom), 4326))
                     from {schema}.{table_to_cover}),
                    (select st_ymin(st_transform(st_collect(geom), 4326))
                     from {schema}.{table_to_cover}),
                    (select st_xmax(st_transform(st_collect(geom), 4326))
                     from {schema}.{table_to_cover}),
                    (select st_ymax(st_transform(st_collect(geom), 4326))
                     from {schema}.{table_to_cover}),
                    4326,
                    {desired_epsg},
                    {desired_epsg}
            );
        """

        self.add_schema(schema)

        self.execute(sql_create_hex_grid)

        self.table_add_spatial_index(new_table_name, schema=schema)

        # TODO: reproject?

    # EXPORT data to file / disk
    # --------------------------

    @timer
    def export_shapefile(
        self,
        table_name: str,
        output_folder: Path,
        where_clause: str = None,
        schema: str = None,
    ) -> gpd.GeoDataFrame:
        """Save a spatial SQL table to shapefile.
           Add an optional filter with the ``where_clause``:
               ``'WHERE speed_limit <= 35'``

        :param table_name: Name of the table to export
        :type table_name: str
        :param output_folder: Folder path to write to
        :type output_folder: Path
        :param where_clause: Any valid SQL where clause, defaults to False
        :type where_clause: str, optional
        """

        if not schema:
            schema = self.ACTIVE_SCHEMA

        self._print(2, f"Exporting {schema}.{table_name} to shapefile")

        query = f"SELECT * FROM {schema}.{table_name} "

        if where_clause:
            query += where_clause
            self._print(1, f"WHERE clause applied: {where_clause}")

        gdf = self.query_as_geo_df(query)

        # Force any boolean columns into strings
        for c in gdf.columns:
            datatype = gdf[c].dtype.name
            if datatype == "bool":
                gdf[c] = gdf[c].astype(str)

        output_path = os.path.join(output_folder, f"{table_name}.shp")
        gdf.to_file(output_path)

        self._print(1, f"Saved to {output_path}")

        return gdf

    def export_all_shapefiles(self, output_folder: Path) -> None:
        """
        Save all spatial tables in the database to shapefile.

        :param output_folder: Folder path to write to
        :type output_folder: Path
        """

        for table in self.all_spatial_tables_as_dict():
            self.export_shapefile(table, output_folder)

    # IMPORT/EXPORT data with shp2pgsql / pgsql2shp
    # ---------------------------------------------
    def pgsql2shp(
        self, table_name: str, output_folder: Path = None, extra_args: list = None
    ) -> Path:
        """
        Use the command-line ``pgsql2shp`` utility.

        TODO: check if pgsql2shp exists and exit early if not
        TODO: check if schema is supported

        ``extra_args`` is a list of tuples, passed in as
        ``[(flag1, val1), (flag2, val2)]``

        For example:
        ``extra_args = [("-g", "custom_geom_column"), ("-b", "")]``

        For more info, see
        http://www.postgis.net/docs/manual-1.3/ch04.html#id436110

        :param table_name: name of the spatial table to dump
        :type table_name: str
        :param output_folder: output folder, defaults to DATA_OUTBOX
        :type output_folder: Path, optional
        :param extra_args: [description], defaults to None
        :type extra_args: list, optional
        :return: path to the newly created shapefile
        :rtype: Path
        """

        # Use the default data outbox if none provided
        if not output_folder:
            output_folder = self.DATA_OUTBOX

        # Put this shapefile into a subfolder
        output_folder = output_folder / table_name
        if not output_folder.exists():
            output_folder.mkdir(parents=True)

        output_file = output_folder / table_name

        # Start out the command
        cmd = f'pgsql2shp -f "{output_file}"'

        # Add the default arguments needed for connecting
        required_args = [
            ("-h", self.HOST),
            ("-p", self.PORT),
            ("-u", self.USER),
            ("-P", self.PASSWORD),
        ]
        for flag, val in required_args:
            cmd += f" {flag} {val}"

        # Add any extra arguments passed in by the user
        if extra_args:
            for flag, val in extra_args:
                cmd += f" {flag} {val}"

        # Finish the command by adding the DB and table names
        cmd += f" {self.DATABASE} {table_name}"

        subprocess.call(cmd, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

        self._print(2, cmd)
        self._print(2, f"Exported {table_name} to {output_file}")

        return output_folder / f"{table_name}.shp"

    def shp2pgsql(self, table_name: str, src_shapefile: Path, new_epsg: int = None) -> str:
        """
        TODO: Docstring
        TODO: add schema option

        :param table_name: [description]
        :type table_name: str
        :param src_shapefile: [description]
        :type src_shapefile: Path
        :param new_epsg: [description], defaults to None
        :type new_epsg: int, optional
        :return: [description]
        :rtype: str
        """

        shapefile_without_extension = str(src_shapefile).replace(".shp", "")

        # TODO: document default settings
        cmd = "shp2pgsql -d -e -I -S"

        # Use geopandas to figure out the source EPSG
        src_epsg = gpd.read_file(src_shapefile).crs.to_epsg()
        if new_epsg:
            cmd += f" -s {src_epsg}:{new_epsg}"
        else:
            cmd += f" -s {src_epsg}"

        cmd += f" {shapefile_without_extension} {table_name}"
        cmd += f" | psql {self.uri()}"

        os.system(cmd)
        return cmd

    # TRANSFER data to another database
    # ---------------------------------

    def transfer_data_to_another_db(
        self, table_name: str, other_postgresql_db, schema: str = None
    ) -> None:
        """
        Copy data from one SQL database to another.

        :param table_name: Name of the table to copy
        :type table_name: str
        :param other_postgresql_db: ``PostgreSQL()`` object for target database
        :type other_postgresql_db: PostgreSQL
        """

        if not schema:
            schema = self.ACTIVE_SCHEMA

        query = f"SELECT * FROM {schema}.{table_name}"

        # If the data is spatial use a geodataframe
        if table_name in self.all_spatial_tables_as_dict():
            gdf = self.query_as_geo_df(query)
            other_postgresql_db.import_geodataframe(gdf, table_name)

        # Otherwise use a normal dataframe
        else:
            df = self.query_as_df(query)
            other_postgresql_db.import_dataframe(df, table_name)


def connect_via_uri(
    uri: str,
    verbosity: str = "full",
    super_db: str = "postgres",
    super_user: str = None,
    super_pw: str = None,
):
    """
    Create a ``PostgreSQL`` object from a URI. Note that
    this process must make assumptions about the super-user
    of the database. Proceed with caution.

    :param uri: database connection string
    :type uri: str
    :param verbosity: level of printout desired, defaults to "full"
    :type verbosity: str, optional
    :param super_db: name of the SQL cluster master DB,
                        defaults to "postgres"
    :type super_db: str, optional
    :return: ``PostgreSQL()`` object
    :rtype: PostgreSQL
    """

    uri_list = uri.split("?")
    base_uri = uri_list[0]

    # Break off the ?sslmode section
    if len(uri_list) > 1:
        sslmode = uri_list[1]
    else:
        sslmode = False

    # Get rid of postgresql://
    base_uri = base_uri.replace(r"postgresql://", "")

    # Split values up to get component parts
    un_pw, host_port_db = base_uri.split("@")
    username, password = un_pw.split(":")
    host, port_db = host_port_db.split(":")
    port, db_name = port_db.split(r"/")

    if not super_pw:
        super_pw = password

    if not super_user:
        super_user = username

    values = {
        "host": host,
        "un": username,
        "pw": password,
        "port": port,
        "sslmode": sslmode,
        "verbosity": "full",
        "super_db": super_db,
        "super_un": super_user,
        "super_pw": super_pw,
    }

    return PostgreSQL(db_name, **values)
