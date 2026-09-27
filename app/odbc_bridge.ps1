# Pont ODBC .NET pour HFSQL (utilisé par l'application, ne pas lancer à la main).
#
# Le pilote ODBC HFSQL plante avec pyodbc mais fonctionne avec System.Data.Odbc (.NET) : ce script lit
# des commandes sur l'entrée standard (une par ligne : op<TAB>arg1_base64<TAB>arg2_base64...) et répond
# une ligne JSON par commande sur la sortie standard. Compatible Windows PowerShell 5.1 et PowerShell 7.
$ErrorActionPreference = "Stop"

$source = @'
using System;
using System.Collections.Generic;
using System.Data;
using System.Data.Odbc;
using System.Globalization;
using System.IO;
using System.Text;

public static class OdbcBridge
{
    static OdbcConnection cnx;
    static OdbcCommand cmd;
    static OdbcDataReader reader;
    static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

    public static void Run()
    {
        var input = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false));
        var output = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false));
        string line;
        while ((line = input.ReadLine()) != null)
        {
            if (line.Length == 0) continue;
            string[] parts = line.Split('\t');
            string op = parts[0];
            var args = new List<string>();
            for (int i = 1; i < parts.Length; i++)
                args.Add(Encoding.UTF8.GetString(Convert.FromBase64String(parts[i])));
            string response;
            try
            {
                response = "{\"ok\":true,\"result\":" + Handle(op, args) + "}";
            }
            catch (Exception ex)
            {
                var message = new StringBuilder();
                for (var e = ex; e != null; e = e.InnerException)
                {
                    if (message.Length > 0) message.Append(" | ");
                    message.Append(e.Message);
                }
                response = "{\"ok\":false,\"error\":" + Str(message.ToString()) + "}";
            }
            output.Write(response);
            output.Write("\n");
            output.Flush();
            if (op == "quit") break;
        }
        Close();
    }

    static void Close()
    {
        try { if (reader != null) reader.Close(); } catch { }
        try { if (cnx != null) cnx.Close(); } catch { }
    }

    static OdbcCommand Command(List<string> a)
    {
        var c = new OdbcCommand(a[0], cnx);
        c.CommandTimeout = 0;
        for (int i = 1; i < a.Count; i++)
            c.Parameters.Add(new OdbcParameter("p" + i, Param(a[i])));
        return c;
    }

    static object Param(string p)
    {
        string v = p.Length > 2 ? p.Substring(2) : "";
        switch (p[0])
        {
            case 'i': return long.Parse(v, Inv);
            case 'f': return double.Parse(v, Inv);
            case 'n': return decimal.Parse(v, Inv);
            case 'b': return v == "1";
            case 'd': return DateTime.Parse(v, Inv, DateTimeStyles.RoundtripKind);
            case 't': return TimeSpan.FromTicks(long.Parse(v, Inv));
            default: return v;
        }
    }

    static string Handle(string op, List<string> a)
    {
        switch (op)
        {
            case "connect":
                cnx = new OdbcConnection(a[0]);
                cnx.ConnectionTimeout = int.Parse(a[1], Inv);
                cnx.Open();
                return "null";
            case "quit":
                return "null";
            case "probe":
                return cnx != null && cnx.State == ConnectionState.Open ? "true" : "false";
            case "getinfo":
                return Info(int.Parse(a[0], Inv));
            case "tables":
            {
                var names = new List<string>();
                DataTable t = cnx.GetSchema("Tables");
                foreach (DataRow r in t.Rows)
                {
                    string type = t.Columns.Contains("TABLE_TYPE") ? Convert.ToString(r["TABLE_TYPE"]) : "TABLE";
                    if (type.ToUpperInvariant() == "TABLE") names.Add(Convert.ToString(r["TABLE_NAME"]));
                }
                var sb = new StringBuilder("[");
                for (int i = 0; i < names.Count; i++) { if (i > 0) sb.Append(','); sb.Append(Str(names[i])); }
                return sb.Append(']').ToString();
            }
            case "describe":
                return Describe(a[0]);
            case "scalar":
                using (var c = Command(a)) return Val(c.ExecuteScalar());
            case "execute":
                if (reader != null) { reader.Close(); reader = null; }
                cmd = Command(a);
                reader = cmd.ExecuteReader();
                return "null";
            case "fetchmany":
            {
                int n = int.Parse(a[0], Inv);
                var sb = new StringBuilder("[");
                int count = 0;
                while (count < n && reader.Read())
                {
                    if (count > 0) sb.Append(',');
                    sb.Append('[');
                    for (int i = 0; i < reader.FieldCount; i++)
                    {
                        if (i > 0) sb.Append(',');
                        sb.Append(reader.IsDBNull(i) ? "null" : Val(reader.GetValue(i)));
                    }
                    sb.Append(']');
                    count++;
                }
                return sb.Append(']').ToString();
            }
            case "close_cursor":
                if (reader != null) { reader.Close(); reader = null; }
                return "null";
        }
        throw new Exception("commande inconnue : " + op);
    }

    static string Info(int code)
    {
        switch (code)
        {
            case 29: // SQL_IDENTIFIER_QUOTE_CHAR
                try
                {
                    string q = new OdbcCommandBuilder().QuoteIdentifier("x", cnx);
                    return Str(q.Length > 1 ? q.Substring(0, q.IndexOf('x')) : "\"");
                }
                catch { return Str("\""); }
            case 17: // SQL_DBMS_NAME
                try { return Str(Convert.ToString(cnx.GetSchema("DataSourceInformation").Rows[0]["DataSourceProductName"])); }
                catch { return Str(cnx.Driver); }
            case 18: // SQL_DBMS_VER
                return Str(cnx.ServerVersion);
        }
        return "null";
    }

    static string Describe(string sql)
    {
        DataTable schema;
        using (var c = new OdbcCommand(sql, cnx))
        {
            OdbcDataReader r;
            try { r = c.ExecuteReader(CommandBehavior.SchemaOnly | CommandBehavior.KeyInfo); }
            catch { r = c.ExecuteReader(CommandBehavior.SchemaOnly); }
            using (r) schema = r.GetSchemaTable();
        }
        var sb = new StringBuilder("[");
        bool first = true;
        foreach (DataRow row in schema.Rows)
        {
            if (!first) sb.Append(',');
            first = false;
            Type type = row["DataType"] as Type;
            int provider = row.Table.Columns.Contains("ProviderType") && row["ProviderType"] != DBNull.Value
                ? Convert.ToInt32(row["ProviderType"]) : -1;
            string kind = Kind(type, provider);
            object precision = row.Table.Columns.Contains("NumericPrecision") ? row["NumericPrecision"] : null;
            object scale = row.Table.Columns.Contains("NumericScale") ? row["NumericScale"] : null;
            bool isKey = row.Table.Columns.Contains("IsKey") && row["IsKey"] != DBNull.Value && Convert.ToBoolean(row["IsKey"]);
            sb.Append('[').Append(Str(Convert.ToString(row["ColumnName"]))).Append(',').Append(Str(kind)).Append(',')
              .Append(Num(precision)).Append(',').Append(Num(scale)).Append(',').Append(isKey ? "true" : "false").Append(']');
        }
        return sb.Append(']').ToString();
    }

    static string Kind(Type t, int provider)
    {
        if (provider == (int)OdbcType.Date) return "date";
        if (provider == (int)OdbcType.Time) return "time";
        if (t == null) return "str";
        if (t == typeof(bool)) return "bool";
        if (t == typeof(byte) || t == typeof(sbyte) || t == typeof(short) || t == typeof(ushort) || t == typeof(int)
            || t == typeof(uint) || t == typeof(long) || t == typeof(ulong)) return "int";
        if (t == typeof(float) || t == typeof(double)) return "float";
        if (t == typeof(decimal)) return "decimal";
        if (t == typeof(DateTime)) return "datetime";
        if (t == typeof(TimeSpan)) return "time";
        if (t == typeof(byte[])) return "bytes";
        return "str";
    }

    static string Num(object v)
    {
        if (v == null || v == DBNull.Value) return "null";
        return Convert.ToInt64(v).ToString(Inv);
    }

    static string Val(object v)
    {
        if (v == null || v == DBNull.Value) return "null";
        if (v is string) return Str((string)v);
        if (v is bool) return (bool)v ? "true" : "false";
        if (v is byte || v is sbyte || v is short || v is ushort || v is int || v is uint || v is long || v is ulong)
            return Convert.ToString(v, Inv);
        if (v is double || v is float)
        {
            double d = Convert.ToDouble(v);
            if (double.IsNaN(d) || double.IsInfinity(d)) return "null";
            return d.ToString("R", Inv);
        }
        if (v is decimal) return "{\"$d\":" + Str(((decimal)v).ToString(Inv)) + "}";
        if (v is DateTime) return "{\"$dt\":" + Str(((DateTime)v).ToString("yyyy-MM-dd'T'HH:mm:ss.ffffff", Inv)) + "}";
        if (v is TimeSpan) return "{\"$t\":" + ((TimeSpan)v).Ticks.ToString(Inv) + "}";
        if (v is byte[]) return "{\"$b\":" + Str(Convert.ToBase64String((byte[])v)) + "}";
        return Str(Convert.ToString(v, Inv));
    }

    static string Str(string s)
    {
        if (s == null) return "null";
        var sb = new StringBuilder("\"");
        foreach (char ch in s)
        {
            switch (ch)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (ch < 0x20) sb.Append("\\u").Append(((int)ch).ToString("x4"));
                    else sb.Append(ch);
                    break;
            }
        }
        return sb.Append('"').ToString();
    }
}
'@

# Références : System.Data (Windows PowerShell 5.1) ou System.Data.Odbc + System.Data.Common (PowerShell 7).
$refs = @(
    [System.Data.Odbc.OdbcConnection].Assembly.Location,
    [System.Data.Common.DbConnection].Assembly.Location,
    [System.Data.DataTable].Assembly.Location,
    [System.ComponentModel.Component].Assembly.Location
) | Where-Object { $_ } | Select-Object -Unique
if ($PSVersionTable.PSVersion.Major -ge 6) {
    # PowerShell 7 : les assemblies par défaut ne sont plus ajoutées quand on en précise.
    $refs += @("System.Runtime", "System.Collections", "System.Console", "System.Data.Common",
               "System.ComponentModel.Primitives", "System.ComponentModel.TypeConverter", "System.Xml.ReaderWriter",
               "System.Text.Encoding.Extensions", "System.Runtime.Extensions", "System.IO", "System.Linq",
               "System.Private.Xml", "netstandard")
}
Add-Type -TypeDefinition $source -ReferencedAssemblies $refs -Language CSharp -WarningAction SilentlyContinue | Out-Null
[OdbcBridge]::Run()
