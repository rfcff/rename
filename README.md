# rename
Rename the class and function names in the specified directorys

## 思路

主要用到find、ctags、awk、sed、sort四个命令的特性
- ctags支持的语言特性，查找类、函数、全局变量、宏等等
- awk语法特性找出对应字符串
- sed执行替换文件对应字符串
- sort对文件相同字符串去重

## 查看支持的语言

- 查看支持的语言以及对应语言的特性

ctags --list-kinds

- 查找指定目录下的cpp/cc/cxx/h结尾的文件

比如: find -E src -path "src/jolog/common" -prune -o -path "src/json" -prune -o -regex '.*\.(cpp|cc|cxx|h)' -type f

find -E '${src_dirs}' '${excludr_dir_str}' -regex '.*\.(cpp|cc|cxx|h)' -type f

- sed全局替换文件

gsed -i "s/\<$funcName\>/$dstFuncName/g"

- awk找出对应类名、函数名....

echo $line|awk -F " " '{print $1}' >> temp/funcs_exclude_name.txt
