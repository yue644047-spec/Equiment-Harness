@ECHO OFF
rem Project 'dify' - entry point for this project's own Neo4j instance.
rem   (the equipment technical-documents QA stack lives in D:\app\dify)
rem service = neo4j-equipment
rem home    = D:\neo4j-equipment
rem data    = D:\neo4j-equipment\data
rem ports   = bolt 7695 / http 7485
rem AUTH IS ENABLED on this instance - connect with a username and password,
rem e.g.   cypher-shell -a bolt://127.0.0.1:7695 -u neo4j -p <your-password>
rem browser = http://127.0.0.1:7485/browser/
rem usage   = <this file> [status|start|stop|restart|console]     default: status
rem (keep this file ASCII-only, and avoid cmd metacharacters in echoed text:
rem  cmd.exe decodes .bat with the OEM code page and treats & as a separator)
call "D:\app\neo4j\neo4j-project.bat" dify %*
